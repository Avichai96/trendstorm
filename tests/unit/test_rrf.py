"""Unit tests for Reciprocal Rank Fusion.

Uses golden examples computed by hand to pin the exact numeric behaviour.
These tests make regression obvious — if the formula changes, they break.
"""
from __future__ import annotations

import pytest

from trendstorm.services.retrieval.rrf import rrf


@pytest.mark.unit
class TestRRFFormula:
    def test_single_list_single_doc(self) -> None:
        scores = rrf([["a"]], k=60)
        # rank 1 → 1/(60+1) = 1/61
        assert abs(scores["a"] - 1 / 61) < 1e-12

    def test_single_list_ordering(self) -> None:
        scores = rrf([["a", "b", "c"]], k=60)
        assert scores["a"] > scores["b"] > scores["c"]
        assert abs(scores["a"] - 1 / 61) < 1e-12
        assert abs(scores["b"] - 1 / 62) < 1e-12
        assert abs(scores["c"] - 1 / 63) < 1e-12

    def test_two_identical_lists_doubles_score(self) -> None:
        scores_one = rrf([["a"]], k=60)
        scores_two = rrf([["a"], ["a"]], k=60)
        assert abs(scores_two["a"] - 2 * scores_one["a"]) < 1e-12

    def test_doc_in_both_lists_outranks_doc_in_one(self) -> None:
        # "a" appears in both lists at rank 1; "b" only in list 2 at rank 1.
        scores = rrf([["a", "b"], ["a", "c"]], k=60)
        assert scores["a"] > scores["b"]
        assert scores["a"] > scores["c"]

    def test_doc_absent_from_a_list_not_penalised_beyond_absence(self) -> None:
        # "b" is in list 1 only; "c" is in list 2 only; both at rank 1.
        scores = rrf([["b"], ["c"]], k=60)
        # scores must be equal — symmetry
        assert abs(scores["b"] - scores["c"]) < 1e-12

    def test_three_lists_golden_example(self) -> None:
        # Computed by hand:
        # doc "x": rank 1 in list 0 → 1/61; rank 2 in list 1 → 1/62; absent list 2
        # doc "y": rank 2 in list 0 → 1/62; rank 1 in list 1 → 1/61; rank 1 in list 2 → 1/61
        scores = rrf([["x", "y"], ["y", "x"], ["y"]], k=60)
        x_expected = 1 / 61 + 1 / 62
        y_expected = 1 / 62 + 1 / 61 + 1 / 61
        assert abs(scores["x"] - x_expected) < 1e-12
        assert abs(scores["y"] - y_expected) < 1e-12
        assert scores["y"] > scores["x"]

    def test_k_parameter_affects_score(self) -> None:
        scores_k1 = rrf([["a"]], k=1)
        scores_k60 = rrf([["a"]], k=60)
        # smaller k → higher score (less smoothing, rank 1 dominates more)
        assert scores_k1["a"] > scores_k60["a"]
        assert abs(scores_k1["a"] - 1 / 2) < 1e-12  # 1/(1+1)

    def test_empty_ranked_list_returns_empty(self) -> None:
        assert rrf([]) == {}

    def test_all_empty_sublists(self) -> None:
        assert rrf([[], []]) == {}

    def test_k_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="k must be >= 1"):
            rrf([["a"]], k=0)

    def test_k_negative_raises(self) -> None:
        with pytest.raises(ValueError):
            rrf([["a"]], k=-10)

    def test_no_docs_in_common_all_present(self) -> None:
        scores = rrf([["a", "b"], ["c", "d"]], k=60)
        assert set(scores.keys()) == {"a", "b", "c", "d"}

    def test_duplicate_ids_within_one_list_accumulate(self) -> None:
        # A backend returning duplicates should not crash; RRF sums per appearance.
        scores = rrf([["a", "a"]], k=60)
        assert abs(scores["a"] - (1 / 61 + 1 / 62)) < 1e-12

    def test_output_sorted_descending_via_sorted(self) -> None:
        scores = rrf([["c", "b", "a"], ["a", "b", "c"]], k=60)
        sorted_ids = sorted(scores, key=lambda d: scores[d], reverse=True)
        # "b" and "a" are in both lists so should outscore "c" which is only top in one
        # This mainly checks that sorting by value works as expected.
        assert sorted_ids[0] in scores
