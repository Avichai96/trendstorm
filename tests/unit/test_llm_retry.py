"""Unit tests for infrastructure/llm/retry.py.

All tests inject _sleep and _random_fn to avoid real delays and make
backoff sequences fully deterministic.
"""
from __future__ import annotations

from typing import Literal

import pytest

from trendstorm.domain.llm.errors import (
    LLMPermanentError,
    LLMRateLimitError,
    LLMSchemaError,
    LLMTransientError,
)
from trendstorm.domain.llm.models import EmbeddingBatchResult
from trendstorm.domain.llm.providers import EmbeddingProvider
from trendstorm.infrastructure.llm.retry import _backoff_delay, with_retry

# ---------------------------------------------------------------------------
# Fake EmbeddingProvider helpers
# ---------------------------------------------------------------------------


def _make_always_succeed() -> EmbeddingProvider:
    class AlwaysSucceed:
        @property
        def model_id(self) -> str:
            return "fake.model"

        @property
        def dimensions(self) -> int:
            return 4

        @property
        def max_batch_size(self) -> int:
            return 10

        @property
        def max_input_tokens(self) -> int:
            return 100

        async def embed_batch(self, texts: list[str], *, task_type: Literal["document", "query"] = "document") -> EmbeddingBatchResult:
            return EmbeddingBatchResult(
                vectors=[[0.1] * 4] * len(texts),
                input_tokens=len(texts),
                model_id="fake.model",
            )

    return AlwaysSucceed()  # type: ignore[return-value]


def _make_fail_then_succeed(n_failures: int) -> tuple[EmbeddingProvider, list[int]]:
    """Provider that raises LLMTransientError n_failures times, then succeeds."""
    calls: list[int] = []

    class FailThenSucceed:
        @property
        def model_id(self) -> str:
            return "fake.model"

        @property
        def dimensions(self) -> int:
            return 4

        @property
        def max_batch_size(self) -> int:
            return 10

        @property
        def max_input_tokens(self) -> int:
            return 100

        async def embed_batch(self, texts: list[str], *, task_type: Literal["document", "query"] = "document") -> EmbeddingBatchResult:
            calls.append(len(calls) + 1)
            if len(calls) <= n_failures:
                raise LLMTransientError(f"transient #{len(calls)}")
            return EmbeddingBatchResult(
                vectors=[[0.1] * 4] * len(texts),
                input_tokens=1,
                model_id="fake.model",
            )

    return FailThenSucceed(), calls  # type: ignore[return-value]


def _make_always_fail(exc: Exception) -> EmbeddingProvider:
    class AlwaysFail:
        @property
        def model_id(self) -> str:
            return "fake.model"

        @property
        def dimensions(self) -> int:
            return 4

        @property
        def max_batch_size(self) -> int:
            return 10

        @property
        def max_input_tokens(self) -> int:
            return 100

        async def embed_batch(self, texts: list[str], *, task_type: Literal["document", "query"] = "document") -> EmbeddingBatchResult:
            raise exc

    return AlwaysFail()  # type: ignore[return-value]


async def _no_sleep(delay: float) -> None:
    """Fake sleep that returns immediately."""


_max_random = lambda a, b: b  # always returns upper bound — deterministic  # noqa: E731
_zero_random = lambda a, b: a  # always returns 0  # noqa: E731


# ---------------------------------------------------------------------------
# Tests: _backoff_delay
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBackoffDelay:
    def test_delay_grows_exponentially(self) -> None:
        delays = [_backoff_delay(1.0, 3600.0, i, _random_fn=_max_random) for i in range(5)]
        assert delays == [1.0, 2.0, 4.0, 8.0, 16.0]

    def test_delay_capped_at_max_delay(self) -> None:
        delay = _backoff_delay(1.0, 3.0, 10, _random_fn=_max_random)
        assert delay == 3.0

    def test_jitter_lower_bound_is_zero(self) -> None:
        delay = _backoff_delay(1.0, 60.0, 0, _random_fn=_zero_random)
        assert delay == 0.0

    def test_random_fn_receives_zero_as_lower_bound(self) -> None:
        received: list[tuple[float, float]] = []

        def capture(a: float, b: float) -> float:
            received.append((a, b))
            return 0.0

        _backoff_delay(2.0, 60.0, 1, _random_fn=capture)
        assert received[0][0] == 0.0
        assert received[0][1] == pytest.approx(4.0)  # min(60, 2.0 * 2^1)

    def test_default_random_fn_within_bounds(self) -> None:
        for attempt in range(5):
            delay = _backoff_delay(1.0, 60.0, attempt)
            cap = min(60.0, 1.0 * (2 ** attempt))
            assert 0.0 <= delay <= cap


# ---------------------------------------------------------------------------
# Tests: retry behaviour
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRetryingEmbeddingProvider:
    async def test_success_on_first_attempt_no_sleep(self) -> None:
        sleep_calls: list[float] = []

        async def recording_sleep(d: float) -> None:
            sleep_calls.append(d)

        provider = _make_always_succeed()
        retrying = with_retry(provider, max_attempts=3, _sleep=recording_sleep)
        result = await retrying.embed_batch(["x"])
        assert result is not None
        assert sleep_calls == []

    async def test_retries_on_transient_error_then_succeeds(self) -> None:
        provider, calls = _make_fail_then_succeed(2)  # fails twice, succeeds on 3rd
        retrying = with_retry(
            provider,
            max_attempts=3,
            _sleep=_no_sleep,
            _random_fn=_zero_random,
        )
        result = await retrying.embed_batch(["x"])
        assert result is not None
        assert len(calls) == 3  # 2 failures + 1 success

    async def test_raises_after_exhausting_all_attempts(self) -> None:
        provider, calls = _make_fail_then_succeed(99)  # always fails in practice
        retrying = with_retry(
            provider,
            max_attempts=3,
            _sleep=_no_sleep,
            _random_fn=_zero_random,
        )
        with pytest.raises(LLMTransientError):
            await retrying.embed_batch(["x"])
        assert len(calls) == 3  # exactly max_attempts calls

    async def test_sleep_called_between_each_retry(self) -> None:
        sleep_delays: list[float] = []

        async def recording_sleep(d: float) -> None:
            sleep_delays.append(d)

        provider, _ = _make_fail_then_succeed(2)
        retrying = with_retry(
            provider,
            max_attempts=3,
            base_delay=1.0,
            _sleep=recording_sleep,
            _random_fn=_max_random,  # deterministic: returns upper bound
        )
        await retrying.embed_batch(["x"])

        # 2 failures → 2 sleeps before retries; no sleep after final success
        assert len(sleep_delays) == 2
        # Full-jitter with _max_random: delay = min(max, base * 2^attempt)
        assert sleep_delays[0] == pytest.approx(1.0)   # attempt 0: min(60, 1.0*1)
        assert sleep_delays[1] == pytest.approx(2.0)   # attempt 1: min(60, 1.0*2)

    async def test_no_sleep_after_final_failed_attempt(self) -> None:
        sleep_calls: list[float] = []

        async def recording_sleep(d: float) -> None:
            sleep_calls.append(d)

        provider = _make_always_fail(LLMTransientError("always"))
        retrying = with_retry(
            provider,
            max_attempts=3,
            _sleep=recording_sleep,
            _random_fn=_zero_random,
        )
        with pytest.raises(LLMTransientError):
            await retrying.embed_batch(["x"])

        # max_attempts=3: sleep after attempt 0, sleep after attempt 1, no sleep after attempt 2
        assert len(sleep_calls) == 2

    async def test_permanent_error_not_retried(self) -> None:
        sleep_calls: list[float] = []

        async def recording_sleep(d: float) -> None:
            sleep_calls.append(d)

        provider = _make_always_fail(LLMPermanentError("permanent"))
        retrying = with_retry(provider, max_attempts=3, _sleep=recording_sleep)
        with pytest.raises(LLMPermanentError):
            await retrying.embed_batch(["x"])
        assert sleep_calls == []

    async def test_schema_error_not_retried(self) -> None:
        provider = _make_always_fail(LLMSchemaError("bad schema"))
        retrying = with_retry(provider, max_attempts=3, _sleep=_no_sleep)
        with pytest.raises(LLMSchemaError):
            await retrying.embed_batch(["x"])

    async def test_rate_limit_error_is_retried(self) -> None:
        """LLMRateLimitError extends LLMTransientError — must be retried."""
        # Swap the transient with a rate-limit error (calls unused; local provider overridden)
        provider, _calls = _make_fail_then_succeed(1)
        provider = _make_always_fail(LLMRateLimitError("rate limit"))
        retrying = with_retry(
            provider, max_attempts=2, _sleep=_no_sleep, _random_fn=_zero_random
        )
        with pytest.raises(LLMRateLimitError):
            await retrying.embed_batch(["x"])

    async def test_max_attempts_one_means_no_retry(self) -> None:
        sleep_calls: list[float] = []

        async def recording_sleep(d: float) -> None:
            sleep_calls.append(d)

        provider = _make_always_fail(LLMTransientError("fail"))
        retrying = with_retry(provider, max_attempts=1, _sleep=recording_sleep)
        with pytest.raises(LLMTransientError):
            await retrying.embed_batch(["x"])
        assert sleep_calls == []


# ---------------------------------------------------------------------------
# Tests: Protocol compliance and property forwarding
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRetryingProviderProtocol:
    def test_satisfies_embedding_provider_protocol(self) -> None:
        retrying = with_retry(_make_always_succeed())
        assert isinstance(retrying, EmbeddingProvider)

    def test_model_id_forwarded(self) -> None:
        assert with_retry(_make_always_succeed()).model_id == "fake.model"

    def test_dimensions_forwarded(self) -> None:
        assert with_retry(_make_always_succeed()).dimensions == 4

    def test_max_batch_size_forwarded(self) -> None:
        assert with_retry(_make_always_succeed()).max_batch_size == 10

    def test_max_input_tokens_forwarded(self) -> None:
        assert with_retry(_make_always_succeed()).max_input_tokens == 100

    def test_retry_wrapping_gemini_provider(self) -> None:
        from tests.unit.test_gemini_embedder import _FakeGeminiClient
        from trendstorm.infrastructure.llm.gemini import GeminiEmbeddingProvider

        base = GeminiEmbeddingProvider(api_key="x", _client=_FakeGeminiClient())
        wrapped = with_retry(base, max_attempts=2)
        assert isinstance(wrapped, EmbeddingProvider)
        assert wrapped.model_id == "gemini.text-embedding-004"
