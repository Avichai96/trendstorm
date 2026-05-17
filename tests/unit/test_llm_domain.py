"""Unit tests for domain/llm/ models, providers, and errors."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from trendstorm.domain.llm.models import Completion, EmbeddingBatchResult, Message
from trendstorm.domain.llm.providers import ChatProvider, EmbeddingProvider


@pytest.mark.unit
class TestMessage:
    def test_valid_roles(self) -> None:
        for role in ("user", "assistant", "system"):
            m = Message(role=role, content="hello")  # type: ignore[arg-type]
            assert m.role == role

    def test_invalid_role_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Message(role="human", content="hello")  # type: ignore[arg-type]

    def test_empty_content_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Message(role="user", content="")

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Message(role="user", content="hi", extra_field="x")  # type: ignore[call-arg]


@pytest.mark.unit
class TestCompletion:
    def test_valid_completion(self) -> None:
        c = Completion(
            content="hello world",
            model_id="gemini.gemini-2.0-flash",
            input_tokens=10,
            output_tokens=5,
        )
        assert c.finish_reason is None

    def test_negative_input_tokens_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Completion(
                content="hi",
                model_id="gemini.gemini-2.0-flash",
                input_tokens=-1,
                output_tokens=5,
            )

    def test_finish_reason_values(self) -> None:
        for reason in ("stop", "length", "content_filter", "tool_calls"):
            c = Completion(
                content="x",
                model_id="openai.gpt-4o-mini",
                input_tokens=0,
                output_tokens=1,
                finish_reason=reason,  # type: ignore[arg-type]
            )
            assert c.finish_reason == reason

    def test_invalid_finish_reason_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Completion(
                content="x",
                model_id="openai.gpt-4o-mini",
                input_tokens=0,
                output_tokens=1,
                finish_reason="unknown_reason",  # type: ignore[arg-type]
            )

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Completion(  # type: ignore[call-arg]
                content="x",
                model_id="x.y",
                input_tokens=0,
                output_tokens=0,
                unexpected="field",
            )


@pytest.mark.unit
class TestEmbeddingBatchResult:
    def test_valid_result(self) -> None:
        result = EmbeddingBatchResult(
            vectors=[[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]],
            input_tokens=20,
            model_id="gemini.text-embedding-004",
        )
        assert len(result.vectors) == 2
        assert result.model_id == "gemini.text-embedding-004"

    def test_empty_batch_allowed(self) -> None:
        result = EmbeddingBatchResult(vectors=[], input_tokens=0, model_id="x.y")
        assert result.vectors == []

    def test_negative_tokens_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EmbeddingBatchResult(vectors=[], input_tokens=-1, model_id="x.y")

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EmbeddingBatchResult(  # type: ignore[call-arg]
                vectors=[], input_tokens=0, model_id="x.y", extra="bad"
            )


@pytest.mark.unit
class TestEmbeddingProviderProtocol:
    def _make_valid_provider(self) -> object:
        class FakeEmbedder:
            @property
            def model_id(self) -> str:
                return "fake.model-v1"

            @property
            def dimensions(self) -> int:
                return 768

            @property
            def max_batch_size(self) -> int:
                return 32

            @property
            def max_input_tokens(self) -> int:
                return 512

            async def embed_batch(self, texts: list[str]) -> EmbeddingBatchResult:
                return EmbeddingBatchResult(
                    vectors=[[0.0] * 768] * len(texts),
                    input_tokens=sum(len(t) for t in texts),
                    model_id=self.model_id,
                )

        return FakeEmbedder()

    def test_runtime_check_passes_for_valid_impl(self) -> None:
        assert isinstance(self._make_valid_provider(), EmbeddingProvider)

    def test_runtime_check_fails_missing_embed_batch(self) -> None:
        class NoEmbedBatch:
            @property
            def model_id(self) -> str:
                return "x"

            @property
            def dimensions(self) -> int:
                return 1

            @property
            def max_batch_size(self) -> int:
                return 1

            @property
            def max_input_tokens(self) -> int:
                return 1

        assert not isinstance(NoEmbedBatch(), EmbeddingProvider)

    def test_runtime_check_fails_missing_property(self) -> None:
        class MissingDimensions:
            @property
            def model_id(self) -> str:
                return "x"

            @property
            def max_batch_size(self) -> int:
                return 1

            @property
            def max_input_tokens(self) -> int:
                return 1

            async def embed_batch(self, texts: list[str]) -> EmbeddingBatchResult:
                return EmbeddingBatchResult(vectors=[], input_tokens=0, model_id="x")

        assert not isinstance(MissingDimensions(), EmbeddingProvider)


@pytest.mark.unit
class TestChatProviderProtocol:
    def _make_valid_provider(self) -> object:
        class FakeChat:
            @property
            def model_id(self) -> str:
                return "fake.chat-v1"

            async def complete(self, messages: list[Message]) -> Completion:
                return Completion(
                    content="hi",
                    model_id=self.model_id,
                    input_tokens=1,
                    output_tokens=1,
                    finish_reason="stop",
                )

            def stream(self, messages: list[Message]) -> object:
                async def _gen() -> object:
                    yield "hi"

                return _gen()

        return FakeChat()

    def test_runtime_check_passes_for_valid_impl(self) -> None:
        assert isinstance(self._make_valid_provider(), ChatProvider)

    def test_runtime_check_fails_missing_complete(self) -> None:
        class NoComplete:
            @property
            def model_id(self) -> str:
                return "x"

        assert not isinstance(NoComplete(), ChatProvider)
