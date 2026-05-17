"""Gemini embedding and chat providers.

Uses the google-genai SDK (synchronous) wrapped in asyncio.to_thread.
Both providers share _map_gemini_error() and construct the client the same way.

GeminiEmbeddingProvider:
    model_id format: "gemini.{model_name}", e.g. "gemini.text-embedding-004".
    Used as part of the Chroma collection name — changing model creates a new collection.
    max_batch_size: 100 inputs, max_input_tokens: 2048, default_dims: 768.

GeminiChatProvider:
    model_id format: "gemini.{model_name}", e.g. "gemini.gemini-2.0-flash".
    No prompt caching — Gemini context caching is a separate billable feature
    with different semantics; we do not expose it here.
    Tool use via function_declarations → FunctionDeclaration → GenerateContentConfig.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, ClassVar, Literal, NoReturn

from trendstorm.domain.llm.errors import (
    LLMPermanentError,
    LLMRateLimitError,
    LLMSchemaError,
    LLMTimeoutError,
    LLMTransientError,
)
from trendstorm.domain.llm.models import Completion, EmbeddingBatchResult, Message, TokenUsage
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)

_GEMINI_MAX_BATCH = 100
_GEMINI_MAX_INPUT_TOKENS = 2048
_DEFAULT_DIMENSIONS = 768


def _map_gemini_error(exc: Exception) -> NoReturn:
    """Map a Gemini SDK exception to a domain LLM error. Always raises."""
    try:
        from google.genai import errors as gemini_errors

        if isinstance(exc, gemini_errors.ClientError):
            code = getattr(exc, "status_code", 0) or getattr(exc, "code", 0)
            if code == 429:
                raise LLMRateLimitError(str(exc)) from exc
            raise LLMPermanentError(str(exc)) from exc
        if isinstance(exc, gemini_errors.ServerError):
            raise LLMTransientError(str(exc)) from exc
    except ImportError:
        pass

    msg = str(exc).lower()
    if "429" in msg or "rate" in msg or "quota" in msg:
        raise LLMRateLimitError(str(exc)) from exc
    if "timeout" in msg or "deadline" in msg:
        raise LLMTimeoutError(str(exc)) from exc
    if "auth" in msg or "401" in msg or "403" in msg:
        raise LLMPermanentError(str(exc)) from exc
    raise LLMTransientError(str(exc)) from exc


class GeminiEmbeddingProvider:
    """EmbeddingProvider backed by Google Gemini text-embedding-004.

    The google-genai SDK is synchronous; each embed_batch call dispatches
    to asyncio.to_thread. Swap to native async when the SDK adds it.

    Pass _client to inject a fake for unit tests; leave None for production
    (the real genai.Client is constructed from api_key).
    """

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-004",
        output_dimensionality: int = _DEFAULT_DIMENSIONS,
        *,
        _client: Any = None,
    ) -> None:
        self._model = model
        self._output_dimensionality = output_dimensionality
        if _client is not None:
            self._client = _client
        else:
            import google.genai as genai  # deferred — only needed for production (not injected-client tests)

            self._client = genai.Client(api_key=api_key)

    # ------------------------------------------------------------------
    # EmbeddingProvider Protocol properties
    # ------------------------------------------------------------------

    @property
    def model_id(self) -> str:
        return f"gemini.{self._model}"

    @property
    def dimensions(self) -> int:
        return self._output_dimensionality

    @property
    def max_batch_size(self) -> int:
        return _GEMINI_MAX_BATCH

    @property
    def max_input_tokens(self) -> int:
        return _GEMINI_MAX_INPUT_TOKENS

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

        task_type is forwarded to Gemini as RETRIEVAL_DOCUMENT / RETRIEVAL_QUERY,
        enabling asymmetric retrieval for better query-document matching.
        """
        if not texts:
            return EmbeddingBatchResult(vectors=[], input_tokens=0, model_id=self.model_id)

        logger.debug("gemini.embed_batch", n_texts=len(texts), model=self._model, task_type=task_type)

        try:
            response = await asyncio.to_thread(self._call_sync, texts, task_type)
            vectors = [list(emb.values) for emb in response.embeddings]
            # Gemini embedding responses omit token counts; estimate from words.
            estimated_tokens = sum(len(t.split()) for t in texts)
            return EmbeddingBatchResult(
                vectors=vectors,
                input_tokens=estimated_tokens,
                model_id=self.model_id,
            )
        except (LLMRateLimitError, LLMPermanentError, LLMTransientError, LLMTimeoutError):
            raise
        except Exception as e:
            _map_gemini_error(e)

    # Gemini SDK task_type mapping
    _TASK_TYPE_MAP: ClassVar[dict[str, str]] = {
        "document": "RETRIEVAL_DOCUMENT",
        "query": "RETRIEVAL_QUERY",
    }

    def _call_sync(self, texts: list[str], task_type: str) -> Any:
        """Run the synchronous SDK embed call inside asyncio.to_thread."""
        from google.genai import types

        config = types.EmbedContentConfig(
            output_dimensionality=self._output_dimensionality,
            task_type=self._TASK_TYPE_MAP.get(task_type, "RETRIEVAL_DOCUMENT"),
        )
        return self._client.models.embed_content(
            model=self._model,
            contents=texts,
            config=config,
        )


# ---------------------------------------------------------------------------
# Gemini finish_reason mapping
# ---------------------------------------------------------------------------

_FINISH_REASON_MAP: dict[str, str] = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "OTHER": "stop",
}


class GeminiChatProvider:
    """ChatProvider backed by Google Gemini (generate_content).

    Uses the synchronous google-genai SDK wrapped in asyncio.to_thread.
    No prompt caching — Gemini context caching is a separate billable feature.

    Roles: the Gemini API uses "user" and "model" (not "assistant"). Domain
    Message.role "assistant" is translated to "model" automatically.

    complete_with_tools() is Anthropic-compatible in its tool definition format
    so the Analyst can swap providers without changing tool schema definitions.

    Pass _client to inject a fake for unit tests; leave None for production.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.0-flash",
        *,
        max_output_tokens: int = 8192,
        temperature: float | None = None,
        _client: Any = None,
    ) -> None:
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        if _client is not None:
            self._client = _client
        else:
            import google.genai as genai

            self._client = genai.Client(api_key=api_key)

    # ------------------------------------------------------------------
    # ChatProvider Protocol
    # ------------------------------------------------------------------

    @property
    def model_id(self) -> str:
        return f"gemini.{self._model}"

    async def complete(self, messages: list[Message]) -> Completion:
        """Return a single non-streaming completion via asyncio.to_thread."""
        logger.debug("gemini.complete", model=self._model, n_messages=len(messages))
        try:
            response = await asyncio.to_thread(self._complete_sync, messages)
        except (LLMRateLimitError, LLMTimeoutError, LLMPermanentError, LLMTransientError):
            raise
        except Exception as exc:
            _map_gemini_error(exc)

        text = response.text or ""
        usage = getattr(response, "usage_metadata", None)
        return Completion(
            content=text,
            model_id=self.model_id,
            input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
            output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
            finish_reason=self._finish_reason(response),
        )

    async def stream(self, messages: list[Message]) -> AsyncIterator[str]:
        """Yield text chunks. Runs sync streaming in a thread; yields collected chunks."""
        try:
            chunks: list[str] = await asyncio.to_thread(self._stream_sync, messages)
        except (LLMRateLimitError, LLMTimeoutError, LLMPermanentError, LLMTransientError):
            raise
        except Exception as exc:
            _map_gemini_error(exc)

        for chunk in chunks:
            yield chunk

    # ------------------------------------------------------------------
    # Gemini-specific: structured output via function calling
    # ------------------------------------------------------------------

    async def complete_with_tools(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        tool_choice: str | None = None,
    ) -> tuple[str, dict[str, Any], TokenUsage]:
        """Structured output via Gemini function calling.

        Accepts the same tool definition format as AnthropicChatProvider so the
        Analyst service can swap providers without changing schema definitions.

        Returns (tool_name, tool_input, token_usage) where token_usage carries
        prompt/candidates token counts from usage_metadata.
        """
        logger.debug("gemini.complete_with_tools", model=self._model, tools=[t.get("name") for t in tools])
        try:
            name, args, in_tok, out_tok = await asyncio.to_thread(
                self._tool_sync, messages, tools, tool_choice
            )
        except (LLMRateLimitError, LLMTimeoutError, LLMPermanentError, LLMTransientError, LLMSchemaError):
            raise
        except Exception as exc:
            _map_gemini_error(exc)

        return name, args, TokenUsage(input_tokens=in_tok, output_tokens=out_tok)

    # ------------------------------------------------------------------
    # Sync helpers (run inside asyncio.to_thread)
    # ------------------------------------------------------------------

    def _build_contents_and_config(
        self,
        messages: list[Message],
        extra_config: dict[str, Any] | None = None,
    ) -> tuple[Any, Any]:
        """Convert domain Messages to Gemini contents + GenerateContentConfig."""
        from google.genai import types

        system_text: str | None = None
        contents: list[Any] = []

        for msg in messages:
            if msg.role == "system":
                system_text = msg.content
            else:
                # "assistant" → "model" for Gemini API
                gemini_role = "model" if msg.role == "assistant" else "user"
                contents.append(
                    types.Content(
                        role=gemini_role,
                        parts=[types.Part.from_text(text=msg.content)],
                    )
                )

        cfg: dict[str, Any] = {"max_output_tokens": self._max_output_tokens}
        if system_text:
            cfg["system_instruction"] = system_text
        if self._temperature is not None:
            cfg["temperature"] = self._temperature
        if extra_config:
            cfg.update(extra_config)

        config = types.GenerateContentConfig(**cfg)
        return contents, config

    def _complete_sync(self, messages: list[Message]) -> Any:
        contents, config = self._build_contents_and_config(messages)
        return self._client.models.generate_content(
            model=self._model, contents=contents, config=config
        )

    def _stream_sync(self, messages: list[Message]) -> list[str]:
        contents, config = self._build_contents_and_config(messages)
        chunks: list[str] = []
        for chunk in self._client.models.generate_content_stream(
            model=self._model, contents=contents, config=config
        ):
            if chunk.text:
                chunks.append(chunk.text)
        return chunks

    def _tool_sync(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        tool_choice: str | None,
    ) -> tuple[str, dict[str, Any], int, int]:
        from google.genai import types

        # Convert Anthropic-style tool defs to Gemini FunctionDeclarations.
        fn_decls = [
            types.FunctionDeclaration(
                name=t["name"],
                description=t.get("description", ""),
                parameters=t.get("input_schema"),
            )
            for t in tools
        ]
        tool_obj = types.Tool(function_declarations=fn_decls)

        # Build function calling config.
        if tool_choice:
            fn_config = types.FunctionCallingConfig(
                mode="ANY",
                allowed_function_names=[tool_choice],
            )
            tc = types.ToolConfig(function_calling_config=fn_config)
        else:
            tc = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="AUTO")
            )

        contents, base_config = self._build_contents_and_config(messages)
        # Re-build with tool settings added.
        from google.genai import types as _t
        config = _t.GenerateContentConfig(
            system_instruction=getattr(base_config, "system_instruction", None),
            max_output_tokens=self._max_output_tokens,
            temperature=self._temperature,
            tools=[tool_obj],
            tool_config=tc,
        )

        response = self._client.models.generate_content(
            model=self._model, contents=contents, config=config
        )

        usage = getattr(response, "usage_metadata", None)
        in_tok = getattr(usage, "prompt_token_count", 0) or 0
        out_tok = getattr(usage, "candidates_token_count", 0) or 0

        # Extract the first function_call part.
        for candidate in (response.candidates or []):
            for part in (candidate.content.parts if candidate.content else []):
                fc = getattr(part, "function_call", None)
                if fc:
                    return fc.name, dict(fc.args), in_tok, out_tok

        raise LLMSchemaError(
            "Gemini function-calling response contained no function_call part",
            context={"model": self._model},
        )

    @staticmethod
    def _finish_reason(response: Any) -> str | None:
        try:
            reason = response.candidates[0].finish_reason
            # In google-genai SDK, finish_reason may be an enum; convert to string.
            return _FINISH_REASON_MAP.get(str(reason).upper().split(".")[-1])
        except (IndexError, AttributeError):
            return None
