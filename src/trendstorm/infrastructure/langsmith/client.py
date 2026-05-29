"""LangSmith client wrapper — lifecycle-managed SDK integration.

Uses the `langsmith` SDK (NOT `langchain`). This is an important distinction:
- `langsmith` — the official observability/evaluation SDK for LangSmith.
- `langchain` — a prompt-engineering framework; we do NOT use it.

The client is optional: if LANGSMITH__API_KEY is unset or empty, writes
degrade gracefully (no-op) while reads raise so callers know the data is
unavailable. This lets CI pass without LangSmith credentials while still
producing a disk-based EvalRunReport.

Lifecycle:
    connect()       — construct the SDK client; verify API key is set.
    health_check()  — return True if the client is configured with a key.
    close()         — no-op; the SDK is stateless per call.

All write methods (push_eval_results) are fire-and-forget: failures are
logged as warnings, not raised, so evaluation pipeline crashes do not prevent
on-disk artifact writes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.domain.evaluation.models import EvalRunReport
    from trendstorm.shared.config import LangSmithSettings

logger = get_logger(__name__)


class LangSmithClient:
    """Thin lifecycle wrapper around the langsmith.Client SDK.

    Provides the connect/health_check/close lifecycle expected by BaseConsumer
    and the API lifespan. All eval-specific writes are best-effort.

    Args:
        settings: LangSmithSettings with api_key and default project.

    """

    def __init__(self, settings: LangSmithSettings) -> None:
        self._settings = settings
        self._client: Any = None
        self._connected = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        api_key = self._settings.api_key.get_secret_value()
        if not api_key:
            logger.info("langsmith.client.no_api_key — operating in no-op write mode")
            self._connected = False
            return

        import langsmith  # deferred — avoids hard dep when key not set

        self._client = langsmith.Client(api_key=api_key)
        self._connected = True
        logger.info("langsmith.client.connected", project=self._settings.project)

    async def close(self) -> None:
        self._client = None
        self._connected = False

    async def health_check(self) -> bool:
        """Return True if the client is configured with a non-empty API key."""
        return self._connected

    # ------------------------------------------------------------------
    # Dataset management
    # ------------------------------------------------------------------

    def create_dataset(self, name: str, description: str = "") -> str | None:
        """Create a LangSmith dataset and return its ID, or None on failure."""
        if not self._connected or self._client is None:
            return None
        try:
            ds = self._client.create_dataset(
                dataset_name=name,
                description=description,
            )
            return str(ds.id)
        except Exception as exc:
            logger.warning("langsmith.create_dataset_failed", name=name, error=str(exc))
            return None

    def list_examples(self, dataset_name: str) -> list[dict[str, Any]]:
        """Return examples from a named dataset, or [] if unavailable."""
        if not self._connected or self._client is None:
            return []
        try:
            examples = list(self._client.list_examples(dataset_name=dataset_name))
            return [e.dict() if hasattr(e, "dict") else vars(e) for e in examples]
        except Exception as exc:
            logger.warning(
                "langsmith.list_examples_failed", dataset_name=dataset_name, error=str(exc)
            )
            return []

    # ------------------------------------------------------------------
    # Eval result upload
    # ------------------------------------------------------------------

    def push_eval_results(self, report: EvalRunReport, project: str | None = None) -> str | None:
        """Push an EvalRunReport summary to LangSmith as a run. Returns URL or None."""
        if not self._connected or self._client is None:
            logger.debug("langsmith.push_eval_results.skipped — no API key")
            return None

        target_project = project or self._settings.project
        try:
            _run = self._client.create_run(
                name=f"eval-{report.suite}-{report.run_id[:8]}",
                run_type="chain",
                inputs={"suite": report.suite, "n_examples": report.n_examples},
                outputs={
                    "n_passed": report.n_passed,
                    "passed": report.passed,
                    "dimension_summaries": [
                        {
                            "dimension": s.dimension,
                            "mean_score": s.mean_score,
                            "pass_rate": s.pass_rate,
                        }
                        for s in report.dimension_summaries
                    ],
                    "threshold_violations": report.threshold_violations,
                },
                project_name=target_project,
                extra={"run_id": report.run_id},
            )
            url = self.get_project_url(target_project)
            logger.info(
                "langsmith.push_eval_results.done",
                run_id=report.run_id,
                project=target_project,
                url=url,
            )
            return url
        except Exception as exc:
            logger.warning(
                "langsmith.push_eval_results_failed",
                run_id=report.run_id,
                error=str(exc),
            )
            return None

    def get_project_url(self, project: str | None = None) -> str | None:
        """Return the LangSmith UI URL for a project, or None if unavailable."""
        if not self._connected or self._client is None:
            return None
        try:
            p = project or self._settings.project
            # Standard LangSmith URL pattern; SDK may expose a method in future versions.
            return f"https://smith.langchain.com/o/default/projects/p/{p}"
        except Exception:
            return None
