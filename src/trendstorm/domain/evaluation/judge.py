"""Judge Protocol and panel aggregation value objects.

LLMJudge       — a single judge in the panel.
JudgeVote      — one judge's score + rationale for a single claim.
PanelAggregation — how to combine votes from multiple judges.
PanelResult    — the aggregated outcome of a panel vote.
PanelInsufficientVotesError — raised when fewer than min_quorum judges succeed.

Design:
    A panel consists of N judges from different providers (default: 3). Each
    judge calls its own LLM and returns a JudgeVote. The panel aggregates
    all valid votes via PanelAggregation (default: MEAN). If fewer than
    min_quorum judges return a valid vote, PanelInsufficientVotesError is raised
    so callers can decide whether to degrade or fail.

    Cheap, diverse-provider models are preferred over one large model:
    diversity eliminates same-model bias and costs less per panel call.
"""
from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    pass


class PanelInsufficientVotesError(Exception):
    """Raised when fewer than min_quorum judges in a panel return a valid vote."""

    def __init__(self, required: int, received: int, errors: list[str] | None = None) -> None:
        self.required = required
        self.received = received
        self.errors = errors or []
        super().__init__(
            f"Panel quorum not met: required {required} votes, got {received}. "
            f"Errors: {self.errors}"
        )


class JudgeVote(BaseModel):
    """One judge's verdict on a single item (insight, analysis, etc.).

    score is in [0, 1]; passed reflects the judge's own binary assessment.
    rationale is the judge's explanation — stored for LangSmith inspection.
    judge_model identifies which model produced this vote (for provenance).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    score: float = Field(..., ge=0.0, le=1.0)
    passed: bool
    rationale: str = ""
    judge_model: str


class PanelAggregation(StrEnum):
    """Strategy for combining votes from multiple judges."""

    MEAN     = "mean"      # arithmetic mean of all valid scores
    MEDIAN   = "median"    # median (more robust to outliers)
    MIN      = "min"       # conservative — worst judge wins
    MAJORITY = "majority"  # passed = majority of judges passed


class PanelResult(BaseModel):
    """Aggregated outcome of a panel vote.

    votes holds all JudgeVotes that succeeded (errors are excluded but logged).
    score and passed are computed by the aggregation strategy.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    votes: list[JudgeVote]
    score: float = Field(..., ge=0.0, le=1.0)
    passed: bool
    aggregation: PanelAggregation
    n_judges: int                   # total judges attempted
    n_valid: int                    # judges that returned a valid vote


@runtime_checkable
class LLMJudge(Protocol):
    """A single judge in the evaluation panel.

    Implementations wrap a single LLM provider. Each judge is given the same
    prompt and scoring rubric; votes are aggregated by the LLMPanel.

    model_id identifies the judge for provenance tracking in LangSmith and
    for the panel composition log (so "all three from OpenAI" is detectable).
    """

    @property
    def model_id(self) -> str:
        """Canonical '{provider}.{model_name}' for this judge."""
        ...

    async def judge(
        self,
        *,
        claim: str,
        evidence: str,
        context: dict[str, Any],
    ) -> JudgeVote:
        """Score a single claim against the provided evidence.

        Args:
            claim:    the assertion to judge (e.g. an Insight.claim).
            evidence: the supporting text (concatenated chunk texts).
            context:  dimension-specific context (e.g. category name, rubric).

        Returns:
            JudgeVote with score in [0, 1] and binary passed judgment.

        Raises:
            LLMRateLimitError / LLMTransientError on provider issues.
            LLMSchemaError if the provider returns malformed structured output.

        """
        ...
