"""Reciprocal Rank Fusion (RRF) for merging multiple ranked lists.

RRF formula (Cormack et al., 2009):
    score(d) = Σ  1 / (k + rank_i(d))
               i

where rank_i(d) is the 1-based position of document d in list i, and k is a
smoothing constant (default 60, from the original paper).

Why RRF and not score normalisation + weighted sum?
    - Score scales vary wildly across backends: BM25 scores are corpus-dependent
      and unbounded; cosine distances are [0, 2]; reranker scores are [0, 1].
      Normalising across these is fragile and requires knowing the min/max of each
      result set at query time.
    - RRF is rank-only: it does not care about the magnitude of the underlying
      scores, only whether document A ranked above document B.
    - The k=60 constant gives a long tail of weight to lower-ranked results,
      preventing a single list from completely dominating.
    - Empirically competitive with learned fusion on TREC benchmarks.

Implementation is a pure function: no I/O, no state.
"""

from __future__ import annotations


def rrf(
    ranked_lists: list[list[str]],
    *,
    k: int = 60,
) -> dict[str, float]:
    """Merge multiple ranked lists via Reciprocal Rank Fusion.

    Args:
        ranked_lists: Each inner list is a ranking of document IDs, ordered
                      from most to least relevant (index 0 = rank 1).
                      IDs may appear in multiple lists; that is the whole point.
        k:            Smoothing constant. k=60 is the canonical default.
                      Increasing k reduces the advantage of top-ranked results.

    Returns:
        A dict mapping document ID → RRF score (higher is better).
        Documents that appeared in more lists and at higher positions score higher.
        Documents absent from all lists are not in the returned dict.

    Raises:
        ValueError: if k < 1 or if ranked_lists is empty.

    """
    if k < 1:
        raise ValueError(f"RRF k must be >= 1, got {k}")

    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank_zero, doc_id in enumerate(ranked):
            rank_one = rank_zero + 1
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank_one)

    return scores
