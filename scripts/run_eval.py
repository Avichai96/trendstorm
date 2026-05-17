"""run_eval.py — CLI entry point for the TrendStorm evaluation harness.

Usage:
    uv run python scripts/run_eval.py --suite fast
    uv run python scripts/run_eval.py --suite full
    uv run python scripts/run_eval.py --suite fast --golden-dir eval/golden
    uv run python scripts/run_eval.py --suite full --project trendstorm-dev

Suites:
    fast   — deterministic evaluators only (citation accuracy + coverage via
             embedding similarity). No LLM judge calls. Suitable for CI without
             LLM API keys.
    full   — all evaluators including LLM panel judges (faithfulness + relevance).
             Requires at least 2 of: ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY.

Exit codes:
    0  — eval passed (no threshold violations)
    1  — eval failed (one or more threshold violations)
    2  — eval errored (configuration, file I/O, or unrecoverable setup failure)

Artifacts:
    artifacts/eval-{timestamp}-{run_id[:8]}.json is always written (even on LLM
    failure) so CI can read the result without LangSmith access.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure src/ is on the path when run from repo root with `uv run python`.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run TrendStorm evaluation harness over golden dataset.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--suite",
        choices=["fast", "full"],
        default="fast",
        help="Eval suite to run. 'fast' skips LLM panel judges.",
    )
    parser.add_argument(
        "--golden-dir",
        default="eval/golden",
        help="Path to the golden examples directory (default: eval/golden).",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="LangSmith project name override (default: uses EvalSettings).",
    )
    parser.add_argument(
        "--artifacts-dir",
        default="artifacts",
        help="Directory to write eval report JSON (default: artifacts/).",
    )
    return parser.parse_args()


def _load_golden_examples(golden_dir: Path) -> list:
    from trendstorm.domain.evaluation.models import GoldenExample

    examples: list[GoldenExample] = []
    for path in sorted(golden_dir.glob("*/example.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            examples.append(GoldenExample.model_validate(data))
        except Exception as exc:
            print(f"[WARN] Could not load {path}: {exc}", file=sys.stderr)
    return examples


def _build_evaluators_fast(settings) -> list:
    """Deterministic evaluators only — no LLM calls."""
    from trendstorm.infrastructure.llm.registry import build_embedding_provider
    from trendstorm.services.evaluation.evaluators.citation import CitationLookupEvaluator
    from trendstorm.services.evaluation.evaluators.coverage import GoldenCoverageEvaluator

    embed = build_embedding_provider(settings)
    return [
        CitationLookupEvaluator(embed),
        GoldenCoverageEvaluator(embed),
    ]


def _build_evaluators_full(settings) -> list:
    """All evaluators including LLM panel judges."""
    from trendstorm.infrastructure.llm.registry import build_chat_provider, build_embedding_provider
    from trendstorm.services.evaluation.evaluators.citation import CitationLookupEvaluator
    from trendstorm.services.evaluation.evaluators.coverage import GoldenCoverageEvaluator
    from trendstorm.services.evaluation.evaluators.faithfulness import LLMPanelFaithfulnessEvaluator
    from trendstorm.services.evaluation.evaluators.relevance import LLMPanelRelevanceEvaluator
    from trendstorm.services.evaluation.panel import LLMPanel

    embed = build_embedding_provider(settings)
    chat = build_chat_provider(settings)
    panel = LLMPanel(judges=[chat], settings=settings.eval)

    return [
        CitationLookupEvaluator(embed),
        GoldenCoverageEvaluator(embed),
        LLMPanelFaithfulnessEvaluator(panel),
        LLMPanelRelevanceEvaluator(panel),
    ]


async def _run(args: argparse.Namespace) -> int:
    from trendstorm.services.evaluation.runner import EvalRunner
    from trendstorm.shared.config import get_settings
    from trendstorm.shared.logging import configure_logging

    configure_logging()
    settings = get_settings()

    golden_dir = Path(args.golden_dir)
    if not golden_dir.is_dir():
        print(f"[ERROR] Golden directory not found: {golden_dir}", file=sys.stderr)
        return 2

    examples = _load_golden_examples(golden_dir)
    if not examples:
        print(f"[ERROR] No golden examples found in {golden_dir}", file=sys.stderr)
        return 2

    print(f"Loaded {len(examples)} golden example(s) from {golden_dir}", flush=True)

    try:
        if args.suite == "fast":
            evaluators = _build_evaluators_fast(settings)
        else:
            evaluators = _build_evaluators_full(settings)
    except Exception as exc:
        print(f"[ERROR] Failed to build evaluators: {exc}", file=sys.stderr)
        return 2

    langsmith_client = None
    try:
        from trendstorm.infrastructure.langsmith.client import LangSmithClient
        langsmith_client = LangSmithClient(settings.langsmith)
        await langsmith_client.connect()
    except Exception as exc:
        print(f"[WARN] LangSmith client unavailable: {exc} — continuing without remote push.", file=sys.stderr)
        langsmith_client = None

    runner = EvalRunner(
        evaluators=evaluators,
        settings=settings.eval,
        langsmith=langsmith_client,
        artifacts_dir=Path(args.artifacts_dir),
    )

    async def _target(example):
        # Golden examples carry no real Analysis — the evaluators that need an
        # Analysis object (faithfulness, relevance) construct a stub from the
        # golden chunks. For the fast suite, the deterministic evaluators use
        # the chunk corpus directly from the example. Return a minimal Analysis.
        from trendstorm.domain.analyses.models import Analysis
        from trendstorm.shared.ids import new_id

        return Analysis(
            id=new_id(),
            tenant_id=example.tenant_id,
            job_id=new_id(),
            category_id=example.category_name,
            summary=(
                f"Analysis of {example.category_name}. "
                + " ".join(example.expected_analysis.summary_keywords if example.expected_analysis else [])
            ),
            insights=[],
            citations=[],
        )

    report = await runner.run_eval(
        dataset=examples,
        target=_target,
        suite=args.suite,
        project=args.project,
    )

    # Print summary
    print(f"\n{'='*60}")
    print(f"Suite:    {report.suite}")
    print(f"Examples: {report.n_examples}")
    print(f"Passed:   {report.n_passed}")
    print(f"Status:   {'PASS' if report.passed else 'FAIL'}")
    if report.dimension_summaries:
        print("\nDimension scores:")
        for s in report.dimension_summaries:
            status = "✓" if s.pass_rate >= 1.0 else "✗"
            print(f"  {status} {s.dimension:<20} mean={s.mean_score:.3f}  pass_rate={s.pass_rate:.3f}  n={s.n_evaluated}")
    if report.threshold_violations:
        print("\nThreshold violations:")
        for v in report.threshold_violations:
            print(f"  ✗ {v}")
    if report.langsmith_url:
        print(f"\nLangSmith: {report.langsmith_url}")
    print(f"{'='*60}\n", flush=True)

    return 0 if report.passed else 1


def main() -> None:
    args = _parse_args()
    try:
        exit_code = asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\n[INTERRUPTED]", file=sys.stderr)
        exit_code = 2
    except Exception as exc:
        print(f"[ERROR] Unhandled exception: {exc}", file=sys.stderr)
        exit_code = 2
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
