"""Parent-child chunker for RAG retrieval.

Strategy:
    1. Split text into sentences using pysbd (sentence boundary detection).
    2. Group sentences into parent windows (~parent_size_tokens tokens).
       Consecutive parents overlap by parent_overlap_sentences to preserve
       cross-boundary context.
    3. Tile each parent into non-overlapping child windows (~child_size_tokens).
       Children are the retrieval unit; parents are fetched for LLM context.

Output layout — interleaved parents + their children:
    [parent_0, child_0a, child_0b, parent_1, child_1a, ...]

    RawChunk.parent_index is None  → this chunk IS a parent
    RawChunk.parent_index == k     → child of the parent at output[k]
    RawChunk.position              → 0-indexed global sequence position

The pipeline (agents/knowledge/pipeline.py) converts RawChunks to domain
Chunk objects, assigns ULIDs, and writes to Mongo + ChromaDB.

Window sizing note:
    The stopping condition uses per-sentence token counts (sum) to decide
    when a window is full. The final RawChunk.token_count is measured on the
    full concatenated window text — these can differ slightly because tiktoken
    tokenises across sentence boundaries differently than within them.
"""
from __future__ import annotations

from dataclasses import dataclass

from trendstorm.agents.knowledge.tokenizer import count_tokens


@dataclass(frozen=True)
class RawChunk:
    """One chunk produced by the chunker — parent or child."""

    text: str
    token_count: int
    position: int            # 0-indexed global position (parents + children interleaved)
    parent_index: int | None  # None = this IS a parent; int = index of parent in the list
    char_start: int
    char_end: int

    @property
    def is_parent(self) -> bool:
        return self.parent_index is None


# ---------------------------------------------------------------------------
# Internal types — not part of the public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Sentence:
    text: str
    token_count: int   # per-sentence count (used for windowing decisions)
    char_start: int
    char_end: int


@dataclass(frozen=True)
class _Window:
    sentences: tuple[_Sentence, ...]
    text: str
    token_count: int   # count on full window text (accurate)
    char_start: int
    char_end: int


# ---------------------------------------------------------------------------
# ParentChildChunker
# ---------------------------------------------------------------------------


class ParentChildChunker:
    """Split text into parent + child chunk windows.

    Parents provide context for the LLM; children are embedded for retrieval.
    """

    def __init__(
        self,
        parent_size_tokens: int = 800,
        child_size_tokens: int = 200,
        parent_overlap_sentences: int = 2,
    ) -> None:
        self.parent_size_tokens = parent_size_tokens
        self.child_size_tokens = child_size_tokens
        self.parent_overlap_sentences = parent_overlap_sentences

    def chunk(self, text: str) -> list[RawChunk]:
        """Split text into interleaved parent and child RawChunks.

        Returns an empty list for blank or whitespace-only input.
        """
        text = text.strip()
        if not text:
            return []

        sentences = self._segment(text)
        if not sentences:
            return []

        parents = self._build_windows(
            sentences,
            self.parent_size_tokens,
            overlap=self.parent_overlap_sentences,
        )

        result: list[RawChunk] = []
        global_pos = 0

        for parent_window in parents:
            parent_idx = len(result)
            result.append(
                RawChunk(
                    text=parent_window.text,
                    token_count=parent_window.token_count,
                    position=global_pos,
                    parent_index=None,
                    char_start=parent_window.char_start,
                    char_end=parent_window.char_end,
                )
            )
            global_pos += 1

            children = self._build_windows(
                list(parent_window.sentences),
                self.child_size_tokens,
                overlap=0,
            )
            for child_window in children:
                result.append(
                    RawChunk(
                        text=child_window.text,
                        token_count=child_window.token_count,
                        position=global_pos,
                        parent_index=parent_idx,
                        char_start=child_window.char_start,
                        char_end=child_window.char_end,
                    )
                )
                global_pos += 1

        return result

    def _segment(self, text: str) -> list[_Sentence]:
        """Split text into sentences via pysbd. Cursor-tracks char offsets."""
        import pysbd  # deferred — only needed in rag dep group

        segmenter = pysbd.Segmenter(language="en", clean=False)
        raw = segmenter.segment(text)

        result: list[_Sentence] = []
        cursor = 0
        for sent in raw:
            if not sent:
                continue
            result.append(
                _Sentence(
                    text=sent,
                    token_count=count_tokens(sent),
                    char_start=cursor,
                    char_end=cursor + len(sent),
                )
            )
            cursor += len(sent)
        return result

    def _build_windows(
        self,
        sentences: list[_Sentence],
        size_tokens: int,
        overlap: int,
    ) -> list[_Window]:
        """Group sentences into windows of ~size_tokens with optional sentence overlap.

        Overlap causes the next window to begin overlap sentences before where
        the current window ended, providing cross-boundary context for parents.
        Children always use overlap=0 (they tile without gaps or overlaps).
        """
        if not sentences:
            return []

        windows: list[_Window] = []
        start = 0

        while start < len(sentences):
            end = start
            token_total = 0

            # Always include at least one sentence per window; stop once
            # the running total meets the size target.
            while end < len(sentences):
                token_total += sentences[end].token_count
                end += 1
                if token_total >= size_tokens:
                    break

            window_sents = sentences[start:end]
            window_text = "".join(s.text for s in window_sents)
            windows.append(
                _Window(
                    sentences=tuple(window_sents),
                    text=window_text,
                    token_count=count_tokens(window_text),
                    char_start=window_sents[0].char_start,
                    char_end=window_sents[-1].char_end,
                )
            )

            if end >= len(sentences):
                break  # consumed all sentences — no further windows needed

            # Rewind by `overlap` sentences so the next window starts with
            # the tail of the current one for context continuity.
            step_back = min(overlap, len(window_sents) - 1)
            start = end - step_back

        return windows
