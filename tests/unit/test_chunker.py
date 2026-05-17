"""Unit tests for ParentChildChunker.

Golden-data section: uses a 5-sentence fixture with parent_size=20, child_size=10,
overlap=1. All token counts and char offsets were computed from the real tiktoken
cl100k_base encoding and verified manually.

Fixture text (100 chars, 32 individual tokens):
    "The cat sat on the mat. The dog ran fast. The bird flew high. The fish swam deep. The fox ran quick."

Sentences (pysbd, clean=False):
    s0 = "The cat sat on the mat. "   char[0:24]   8 tokens
    s1 = "The dog ran fast. "         char[24:42]  6 tokens
    s2 = "The bird flew high. "       char[42:62]  6 tokens
    s3 = "The fish swam deep. "       char[62:82]  7 tokens
    s4 = "The fox ran quick."         char[82:100] 5 tokens

Expected windows with parent_size=20, child_size=10, overlap=1:
    Parent 0: s0+s1+s2  token_count=18  char[0:62]
    Child 0a: s0+s1     token_count=13  char[0:42]   parent_index=0
    Child 0b: s2        token_count=6   char[42:62]  parent_index=0
    Parent 1: s2+s3+s4  token_count=16  char[42:100]
    Child 1a: s2+s3     token_count=12  char[42:82]  parent_index=3
    Child 1b: s4        token_count=5   char[82:100] parent_index=3
"""
from __future__ import annotations

import pytest

from trendstorm.agents.knowledge.chunker import ParentChildChunker, RawChunk

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

_FIXTURE_TEXT = (
    "The cat sat on the mat. "
    "The dog ran fast. "
    "The bird flew high. "
    "The fish swam deep. "
    "The fox ran quick."
)

# Small limits that produce a predictable, verifiable output.
_SMALL = ParentChildChunker(
    parent_size_tokens=20,
    child_size_tokens=10,
    parent_overlap_sentences=1,
)


# ---------------------------------------------------------------------------
# Golden-data tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGoldenOutput:
    def _chunks(self) -> list[RawChunk]:
        return _SMALL.chunk(_FIXTURE_TEXT)

    def test_produces_six_chunks(self) -> None:
        assert len(self._chunks()) == 6

    def test_positions_are_sequential(self) -> None:
        positions = [c.position for c in self._chunks()]
        assert positions == list(range(6))

    def test_two_parents(self) -> None:
        parents = [c for c in self._chunks() if c.is_parent]
        assert len(parents) == 2

    def test_four_children(self) -> None:
        children = [c for c in self._chunks() if not c.is_parent]
        assert len(children) == 4

    def test_parent_0_text(self) -> None:
        chunks = self._chunks()
        assert chunks[0].text == "The cat sat on the mat. The dog ran fast. The bird flew high. "

    def test_parent_0_char_offsets(self) -> None:
        c = self._chunks()[0]
        assert c.char_start == 0
        assert c.char_end == 62

    def test_parent_0_token_count(self) -> None:
        # Joined-text count differs from sum of individual sentence counts
        assert self._chunks()[0].token_count == 18

    def test_child_0a_text(self) -> None:
        assert self._chunks()[1].text == "The cat sat on the mat. The dog ran fast. "

    def test_child_0a_parent_index(self) -> None:
        assert self._chunks()[1].parent_index == 0

    def test_child_0a_char_offsets(self) -> None:
        c = self._chunks()[1]
        assert c.char_start == 0
        assert c.char_end == 42

    def test_child_0a_token_count(self) -> None:
        assert self._chunks()[1].token_count == 13

    def test_child_0b_text(self) -> None:
        assert self._chunks()[2].text == "The bird flew high. "

    def test_child_0b_parent_index(self) -> None:
        assert self._chunks()[2].parent_index == 0

    def test_child_0b_token_count(self) -> None:
        assert self._chunks()[2].token_count == 6

    def test_parent_1_text(self) -> None:
        chunks = self._chunks()
        assert chunks[3].text == "The bird flew high. The fish swam deep. The fox ran quick."

    def test_parent_1_char_offsets(self) -> None:
        c = self._chunks()[3]
        assert c.char_start == 42
        assert c.char_end == 100

    def test_parent_1_token_count(self) -> None:
        assert self._chunks()[3].token_count == 16

    def test_parent_1_is_parent(self) -> None:
        assert self._chunks()[3].is_parent is True

    def test_child_1a_parent_index_is_3(self) -> None:
        assert self._chunks()[4].parent_index == 3

    def test_child_1a_token_count(self) -> None:
        assert self._chunks()[4].token_count == 12

    def test_child_1b_text(self) -> None:
        assert self._chunks()[5].text == "The fox ran quick."

    def test_child_1b_parent_index_is_3(self) -> None:
        assert self._chunks()[5].parent_index == 3

    def test_child_1b_token_count(self) -> None:
        assert self._chunks()[5].token_count == 5

    def test_child_1b_char_offsets(self) -> None:
        c = self._chunks()[5]
        assert c.char_start == 82
        assert c.char_end == 100


# ---------------------------------------------------------------------------
# Structural invariant tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStructuralInvariants:
    def test_empty_text_returns_empty(self) -> None:
        assert _SMALL.chunk("") == []

    def test_whitespace_only_returns_empty(self) -> None:
        assert _SMALL.chunk("   \n\t  ") == []

    def test_positions_always_sequential(self) -> None:
        chunks = _SMALL.chunk(_FIXTURE_TEXT)
        for i, c in enumerate(chunks):
            assert c.position == i

    def test_child_parent_index_points_to_parent(self) -> None:
        chunks = _SMALL.chunk(_FIXTURE_TEXT)
        for c in chunks:
            if c.parent_index is not None:
                assert chunks[c.parent_index].is_parent

    def test_all_children_belong_to_existing_parents(self) -> None:
        chunks = _SMALL.chunk(_FIXTURE_TEXT)
        parent_positions = {c.position for c in chunks if c.is_parent}
        for c in chunks:
            if c.parent_index is not None:
                assert chunks[c.parent_index].position in parent_positions

    def test_parents_cover_full_text(self) -> None:
        chunks = _SMALL.chunk(_FIXTURE_TEXT.strip())
        parents = [c for c in chunks if c.is_parent]
        # First parent starts at 0, last parent ends at len(text)
        assert parents[0].char_start == 0
        assert parents[-1].char_end == len(_FIXTURE_TEXT.strip())

    def test_parent_contains_children_text(self) -> None:
        chunks = _SMALL.chunk(_FIXTURE_TEXT)
        for c in chunks:
            if c.parent_index is not None:
                parent = chunks[c.parent_index]
                assert c.text in parent.text

    def test_child_char_offsets_within_parent(self) -> None:
        chunks = _SMALL.chunk(_FIXTURE_TEXT)
        for c in chunks:
            if c.parent_index is not None:
                parent = chunks[c.parent_index]
                assert c.char_start >= parent.char_start
                assert c.char_end <= parent.char_end

    def test_token_counts_positive(self) -> None:
        for c in _SMALL.chunk(_FIXTURE_TEXT):
            assert c.token_count > 0


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEdgeCases:
    def test_single_sentence_produces_one_parent_one_child(self) -> None:
        text = "Artificial intelligence is transforming industries."
        chunks = ParentChildChunker().chunk(text)
        parents = [c for c in chunks if c.is_parent]
        children = [c for c in chunks if not c.is_parent]
        assert len(parents) == 1
        assert len(children) == 1
        assert children[0].parent_index == 0

    def test_single_sentence_parent_and_child_have_same_text(self) -> None:
        text = "Short sentence."
        chunks = ParentChildChunker().chunk(text)
        parents = [c for c in chunks if c.is_parent]
        children = [c for c in chunks if not c.is_parent]
        assert parents[0].text == children[0].text

    def test_default_parameters(self) -> None:
        chunker = ParentChildChunker()
        assert chunker.parent_size_tokens == 800
        assert chunker.child_size_tokens == 200
        assert chunker.parent_overlap_sentences == 2

    def test_very_large_parent_limit_produces_one_parent(self) -> None:
        chunker = ParentChildChunker(parent_size_tokens=10_000, child_size_tokens=5_000)
        chunks = chunker.chunk(_FIXTURE_TEXT)
        parents = [c for c in chunks if c.is_parent]
        assert len(parents) == 1

    def test_is_parent_property(self) -> None:
        chunks = _SMALL.chunk(_FIXTURE_TEXT)
        for c in chunks:
            assert c.is_parent == (c.parent_index is None)
