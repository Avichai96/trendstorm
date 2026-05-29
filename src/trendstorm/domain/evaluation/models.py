"""Evaluation domain models.

EvalDimension      — the four rubric dimensions the panel scores.
EvaluationResult   — per-dimension scores + aggregate for one analysis.
GoldenExample      — a git-versioned fixture with inputs + expected output.
ExpectedAnalysis   — the expected_analysis embedded in a GoldenExample.
EvalRunReport      — summary of one full eval run (dataset x evaluators).

Design notes:
- All models are extra="forbid" (closed schemas catch typos at validation).
- EvaluationResult scores are in [0, 1]; None means the evaluator was skipped
  (e.g. GoldenCoverageEvaluator is skipped on production samples with no golden).
- GoldenExample and ExpectedAnalysis are loaded from eval/golden/ JSON files,
  not from Mongo. LangSmith is for the UI; git is the source of truth.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from trendstorm.shared.ids import new_id

# ---------------------------------------------------------------------------
# EvalDimension
# ---------------------------------------------------------------------------


class EvalDimension(StrEnum):
    """The four rubric dimensions scored per analysis.

    FAITHFULNESS     — every claim is supported by the cited chunks.
    CITATION_ACCURACY — cited chunks actually contain the excerpted text.
    RELEVANCE        — the analysis addresses the category's topic and keywords.
    COVERAGE         — expected insights from the golden example are present.
    """

    FAITHFULNESS = "faithfulness"
    CITATION_ACCURACY = "citation_accuracy"
    RELEVANCE = "relevance"
    COVERAGE = "coverage"


# ---------------------------------------------------------------------------
# EvaluationResult
# ---------------------------------------------------------------------------


class DimensionScore(BaseModel):
    """Score for a single dimension from a single evaluator run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dimension: EvalDimension
    score: float = Field(..., ge=0.0, le=1.0)
    passed: bool
    rationale: str = ""


class EvaluationResult(BaseModel):
    """Evaluation outcome for one analysis — all dimensions that were run.

    dimension_scores holds one DimensionScore per dimension that was evaluated.
    Dimensions not in dimension_scores were skipped (e.g. COVERAGE on prod samples).
    aggregate_score is the mean of all dimension scores that are present.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    tenant_id: str
    analysis_id: str
    job_id: str
    evaluator_version: str = "v1"
    dimension_scores: list[DimensionScore] = Field(default_factory=list)
    aggregate_score: float = Field(default=0.0, ge=0.0, le=1.0)
    flagged: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def score_for(self, dimension: EvalDimension) -> float | None:
        """Return the score for a specific dimension, or None if not evaluated."""
        for ds in self.dimension_scores:
            if ds.dimension == dimension:
                return ds.score
        return None

    def passed_all(self, thresholds: dict[EvalDimension, float]) -> bool:
        """Return True if every evaluated dimension meets its threshold."""
        for ds in self.dimension_scores:
            threshold = thresholds.get(ds.dimension, 0.0)
            if ds.score < threshold:
                return False
        return True


# ---------------------------------------------------------------------------
# GoldenExample — git-versioned fixture
# ---------------------------------------------------------------------------


class ExpectedInsight(BaseModel):
    """One expected insight in a golden example.

    Used by GoldenCoverageEvaluator to compute recall against the actual
    analysis. The claim text is compared via embedding similarity — exact
    wording need not match.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim: str
    required: bool = True  # if True, missing this insight fails COVERAGE
    keywords: list[str] = Field(default_factory=list)


class ExpectedAnalysis(BaseModel):
    """The expected analysis outputs for a golden example.

    The evaluator checks that the actual Analysis contains insights that
    semantically match each ExpectedInsight (embedding similarity ≥ threshold).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary_keywords: list[str] = Field(default_factory=list)
    insights: list[ExpectedInsight] = Field(default_factory=list)
    min_citations: int = Field(default=1, ge=0)


class GoldenChunk(BaseModel):
    """A single chunk of evidence in a golden example.

    Stored inline in the golden JSON — no Mongo or ChromaDB lookup needed.
    chunk_id must match the supporting_chunk_ids in the expected analysis.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str
    document_id: str
    source_id: str
    text: str
    source_url: str | None = None


class GoldenExample(BaseModel):
    """A single git-versioned evaluation fixture.

    Loaded from eval/golden/{name}/example.json. The directory structure is:
        eval/golden/
            {example_name}/
                example.json   ← this model
                README.md      ← optional curation notes

    category_name and category_description define the retrieval context.
    chunks are the evidence corpus that should ground the analysis.
    expected_analysis defines the coverage targets for GoldenCoverageEvaluator.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    name: str
    description: str = ""
    tenant_id: str = "golden-tenant"
    category_name: str
    category_description: str = ""
    category_keywords: list[str] = Field(default_factory=list)
    chunks: list[GoldenChunk] = Field(default_factory=list)
    expected_analysis: ExpectedAnalysis | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# EvalRunReport
# ---------------------------------------------------------------------------


class DimensionSummary(BaseModel):
    """Aggregate stats for one dimension across all examples in a run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dimension: EvalDimension
    mean_score: float = Field(..., ge=0.0, le=1.0)
    pass_rate: float = Field(..., ge=0.0, le=1.0)
    n_evaluated: int = Field(..., ge=0)


class EvalRunReport(BaseModel):
    """Summary of a complete eval run (dataset x evaluators).

    Persisted to disk as artifacts/eval-{timestamp}.json and pushed to
    LangSmith if the client is configured. CI reads the on-disk artifact
    so the gate works even when LangSmith is unreachable.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(default_factory=new_id)
    suite: str  # "fast" | "full" | "production"
    n_examples: int = Field(..., ge=0)
    n_passed: int = Field(..., ge=0)
    dimension_summaries: list[DimensionSummary] = Field(default_factory=list)
    threshold_violations: list[str] = Field(default_factory=list)
    langsmith_url: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    elapsed_seconds: float | None = None

    @property
    def passed(self) -> bool:
        return len(self.threshold_violations) == 0

    def summary_for(self, dimension: EvalDimension) -> DimensionSummary | None:
        for s in self.dimension_summaries:
            if s.dimension == dimension:
                return s
        return None
