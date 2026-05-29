"""LLM retry wrapper — exponential backoff with full jitter.

Only LLMTransientError (and subclasses: LLMRateLimitError, LLMTimeoutError)
trigger retries. LLMPermanentError and LLMSchemaError propagate immediately.

Backoff formula (full jitter, per AWS architecture blog):
    cap   = min(max_delay, base * 2 ** attempt)
    delay = uniform(0, cap)

This spreads retrying clients across time (avoids thundering-herd) while
still growing the upper bound exponentially with each failure.

Usage (via registry, Step 8):
    provider = GeminiEmbeddingProvider(api_key=...)
    provider = with_retry(provider, max_attempts=3, base_delay=1.0, max_delay=60.0)
"""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING, Literal

from trendstorm.domain.llm.errors import LLMTransientError
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from trendstorm.domain.llm.models import EmbeddingBatchResult
    from trendstorm.domain.llm.providers import EmbeddingProvider

logger = get_logger(__name__)


def _backoff_delay(
    base: float,
    max_delay: float,
    attempt: int,
    *,
    _random_fn: Callable[[float, float], float] | None = None,
) -> float:
    """Full-jitter exponential delay: uniform(0, min(max_delay, base * 2**attempt))."""
    fn = _random_fn if _random_fn is not None else random.uniform
    cap = min(max_delay, base * (2**attempt))
    return fn(0.0, cap)


class RetryingEmbeddingProvider:
    """Wraps any EmbeddingProvider with transparent retry logic.

    Catches LLMTransientError, sleeps with exponential-backoff full jitter,
    and retries up to max_attempts times total (1 original + N-1 retries).
    After exhausting all attempts the last LLMTransientError is re-raised.

    _sleep and _random_fn are injected in tests to avoid real delays and
    make backoff sequences deterministic.
    """

    def __init__(
        self,
        provider: EmbeddingProvider,
        *,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        _sleep: Callable[[float], Awaitable[None]] | None = None,
        _random_fn: Callable[[float, float], float] | None = None,
    ) -> None:
        self._provider = provider
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._sleep: Callable[[float], Awaitable[None]] = (
            _sleep if _sleep is not None else asyncio.sleep
        )
        self._random_fn = _random_fn

    # ------------------------------------------------------------------
    # EmbeddingProvider Protocol properties — forwarded to wrapped provider
    # ------------------------------------------------------------------

    @property
    def model_id(self) -> str:
        return self._provider.model_id

    @property
    def dimensions(self) -> int:
        return self._provider.dimensions

    @property
    def max_batch_size(self) -> int:
        return self._provider.max_batch_size

    @property
    def max_input_tokens(self) -> int:
        return self._provider.max_input_tokens

    # ------------------------------------------------------------------
    # EmbeddingProvider Protocol method
    # ------------------------------------------------------------------

    async def embed_batch(
        self,
        texts: list[str],
        *,
        task_type: Literal["document", "query"] = "document",
    ) -> EmbeddingBatchResult:
        """Embed with retry. Delegates to the wrapped provider."""
        last_exc: LLMTransientError | None = None

        for attempt in range(self._max_attempts):
            try:
                return await self._provider.embed_batch(texts, task_type=task_type)
            except LLMTransientError as e:
                last_exc = e
                remaining = self._max_attempts - attempt - 1
                if remaining > 0:
                    delay = _backoff_delay(
                        self._base_delay,
                        self._max_delay,
                        attempt,
                        _random_fn=self._random_fn,
                    )
                    logger.warning(
                        "llm.embed_batch.retry",
                        attempt=attempt + 1,
                        max_attempts=self._max_attempts,
                        remaining=remaining,
                        delay_s=round(delay, 3),
                        model_id=self._provider.model_id,
                        error=str(e),
                    )
                    await self._sleep(delay)

        raise last_exc  # type: ignore[misc]  # always set: loop runs >= 1 time


def with_retry(
    provider: EmbeddingProvider,
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    _sleep: Callable[[float], Awaitable[None]] | None = None,
    _random_fn: Callable[[float, float], float] | None = None,
) -> EmbeddingProvider:
    """Wrap an EmbeddingProvider with exponential-backoff retry.

    The returned object satisfies the EmbeddingProvider Protocol.
    LLMPermanentError and LLMSchemaError are never retried.
    """
    return RetryingEmbeddingProvider(
        provider,
        max_attempts=max_attempts,
        base_delay=base_delay,
        max_delay=max_delay,
        _sleep=_sleep,
        _random_fn=_random_fn,
    )
