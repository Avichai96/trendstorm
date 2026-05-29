"""Ollama embedding provider (local models).

Uses ollama.AsyncClient — native async, no thread-pool wrapping needed.
Token counts come from response.prompt_eval_count when available.

Default model: nomic-embed-text (768 dims, 8192 token context).
model_id format: "ollama.{model_name}", e.g. "ollama.nomic-embed-text".

Error semantics differ from cloud providers:
    ResponseError with "not found" → LLMPermanentError (model not pulled)
    Connection errors              → LLMTransientError (Ollama not running)
    Other errors                   → LLMTransientError
"""

from __future__ import annotations

from typing import Any, Literal

import ollama

from trendstorm.domain.llm.errors import (
    LLMPermanentError,
    LLMTransientError,
)
from trendstorm.domain.llm.models import EmbeddingBatchResult
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_HOST = "http://localhost:11434"
_DEFAULT_MODEL = "nomic-embed-text"
_DEFAULT_DIMENSIONS = 768
_OLLAMA_MAX_BATCH = 64  # local model; no hard API limit, but keep batches modest
_OLLAMA_MAX_INPUT_TOKENS = 8192


class OllamaEmbeddingProvider:
    """EmbeddingProvider backed by a locally-running Ollama model.

    Uses ollama.AsyncClient for native async calls.
    Pass _client to inject a fake for unit tests.
    """

    def __init__(
        self,
        host: str = _DEFAULT_HOST,
        model: str = _DEFAULT_MODEL,
        output_dimensionality: int = _DEFAULT_DIMENSIONS,
        *,
        _client: Any = None,
    ) -> None:
        self._model = model
        self._output_dimensionality = output_dimensionality
        self._client = _client if _client is not None else ollama.AsyncClient(host=host)

    # ------------------------------------------------------------------
    # EmbeddingProvider Protocol properties
    # ------------------------------------------------------------------

    @property
    def model_id(self) -> str:
        return f"ollama.{self._model}"

    @property
    def dimensions(self) -> int:
        return self._output_dimensionality

    @property
    def max_batch_size(self) -> int:
        return _OLLAMA_MAX_BATCH

    @property
    def max_input_tokens(self) -> int:
        return _OLLAMA_MAX_INPUT_TOKENS

    # ------------------------------------------------------------------
    # EmbeddingProvider Protocol method
    # ------------------------------------------------------------------

    async def embed_batch(
        self,
        texts: list[str],
        *,
        task_type: Literal["document", "query"] = "document",
    ) -> EmbeddingBatchResult:
        """Embed a batch of texts. len(result.vectors) == len(texts).

        task_type is accepted for Protocol compatibility but ignored — Ollama
        local models use symmetric embeddings.
        """
        if not texts:
            return EmbeddingBatchResult(vectors=[], input_tokens=0, model_id=self.model_id)

        logger.debug("ollama.embed_batch", n_texts=len(texts), model=self._model)

        try:
            response = await self._client.embed(model=self._model, input=texts)
            vectors = [list(v) for v in response.embeddings]
            # Ollama returns prompt_eval_count when available; fall back to word estimate.
            input_tokens = response.prompt_eval_count or sum(len(t.split()) for t in texts)
            return EmbeddingBatchResult(
                vectors=vectors,
                input_tokens=input_tokens,
                model_id=self.model_id,
            )
        except ollama.ResponseError as e:
            msg = str(e).lower()
            if "not found" in msg or "does not exist" in msg or "404" in msg:
                raise LLMPermanentError(
                    f"Ollama model '{self._model}' not found — run `ollama pull {self._model}`",
                    context={"model": self._model, "error": str(e)},
                ) from e
            raise LLMTransientError(str(e)) from e
        except ollama.RequestError as e:
            raise LLMPermanentError(str(e)) from e
        except OSError as e:
            raise LLMTransientError(
                f"Cannot reach Ollama at configured host — is it running? ({e})",
            ) from e
        except Exception as e:
            raise LLMTransientError(str(e)) from e
