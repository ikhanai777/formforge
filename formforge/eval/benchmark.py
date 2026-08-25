"""The benchmark runner and its metrics (spec section 13.2).

    python -m formforge.eval.benchmark
    python -m formforge.eval.benchmark --difficulty easy --json out.json
    python -m formforge.eval.benchmark --baseline previous.json

You cannot improve this system without a benchmark, and the reason is specific:
almost every change here is a change to a *prompt*, a *DFM constant*, or a
*template* -- none of which have types, and all of which can regress silently.
A prompt edit is a code change and needs the same discipline (spec section
13.3), which is what `--baseline` is for: it compares two runs and fails the
build when any metric drops more than the allowed margin.

The metric that is deliberately absent is intent match, which the spec scores
1-5 by human rating. A model rating its own output would produce a number that
looks like the others and means nothing, so this reports only what it can
measure. The vision critique's verdict is recorded alongside, clearly labelled
as a proxy.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..dfm import DEFAULT_PROFILE_ID
from ..llm import build_client
from ..orchestrator import Orchestrator
from ..registry import TemplateRegistry
from .prompts import ALL_CASES, BenchmarkCase, cases

# Targets from spec section 13.2. A run below any of these is not shippable.
TARGETS: dict[str, float] = {
    "manifold_rate": 0.99,
    "dfm_pass_rate": 0.95,
    "first_pass_rate": 0.70,
    "mean_iterations": 1.6,  # lower is better; handled specially
    "dimensional_fidelity": 0.97,
    "support_free_rate": 0.80,
    "refusal_accuracy": 1.00,
    "clarification_accuracy": 0.90,
    "template_hit_rate": 0.60,
}
# Metrics where a lower number is better.
LOWER_IS_BETTER = {"mean_iterations", "p50_latency_s", "p95_latency_s", "cost_per_accepted_usd"}

# How far a metric may drop before a regression gate fails it (spec 13.3).
REGRESSION_MARGIN = 0.02

# Metrics measured in seconds or dollars rather than as a fraction, where the
# same absolute margin means something entirely different. Two points of
# accuracy is a real regression; twenty milliseconds of wall clock is the
# machine being busy. Holding a latency to +/-0.02 s produces a gate that fails
# on noise, which is worse than no gate at all -- it gets muted, and then the
# real regressions go through with it. These get a proportional band instead.
NOISY_METRICS = {"p50_latency_s", "p95_latency_s", "cost_per_accepted_usd"}
NOISE_FRACTION = 0.25


@dataclass
class CaseOutcome:
    """What happened on one benchmark case."""

    case_id: str
    difficulty: str
    ok: bool
    status: str
    duration_s: float
    iterations: int = 0
    template_id: str | None = None
    route: str = ""
    # Did this case produce a mesh? A case that never built has no geometry to
    # be manifold or unprintable, and folding it into those rates conflates
    # "the geometry was bad" with "the geometry was never attempted".
    built: bool = False
    manifold: bool = False
    dfm_passed: bool = False
    # Set when the case could not run for want of a capability rather than
    # because anything was wrong with it -- e.g. freeform with no API key.
    unavailable: bool = False
    dimensions_correct: bool | None = None
    routed_correctly: bool | None = None
    refusal_correct: bool | None = None
    clarification_correct: bool | None = None
    overhang_fraction: float | None = None
    cost_usd: float = 0.0
    message: str = ""

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class BenchmarkReport:
    """Aggregated metrics over a run."""

    outcomes: list[CaseOutcome] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    started_at: str = ""
    duration_s: float = 0.0
    offline: bool = True

    def as_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "duration_s": round(self.duration_s, 2),
            "offline": self.offline,
            "cases": len(self.outcomes),
            "metrics": self.metrics,
            "targets": TARGETS,
            "outcomes": [o.as_dict() for o in self.outcomes],
        }

    def failing_targets(self) -> dict[str, tuple[float, float]]:
        """Metrics below (or, for the inverted ones, above) their target."""
        failing: dict[str, tuple[float, float]] = {}
        for name, target in TARGETS.items():
            value = self.metrics.get(name)
            if value is None:
                continue
            missed = value > target if name in LOWER_IS_BETTER else value < target
            if missed:
                failing[name] = (value, target)
        return failing

    def render(self) -> str:
        lines = [
            f"FormForge benchmark -- {len(self.outcomes)} cases in {self.duration_s:.0f}s"
            + ("  (offline: template path only)" if self.offline else ""),
            "",
        ]
        failing = self.failing_targets()
        for name, value in self.metrics.items():
            target = TARGETS.get(name)
            if target is None:
                lines.append(f"  {name:<26} {_fmt(name, value)}")
                continue
            marker = "FAIL" if name in failing else "ok  "
            lines.append(
                f"  {name:<26} {_fmt(name, value):>9}   target "
                f"{'<=' if name in LOWER_IS_BETTER else '>='} {_fmt(name, target)}  [{marker}]"
            )

        unavailable = [o for o in self.outcomes if o.unavailable]
        if unavailable:
            lines += [
                "",
                f"{len(unavailable)} case(s) could not run and are excluded from "
                "every rate above:",
            ]
            for outcome in unavailable[:20]:
                lines.append(f"  {outcome.case_id:<28} {outcome.message[:70]}")

        failures = [o for o in self.outcomes if not o.ok and not o.unavailable]
        if failures:
            lines += ["", f"{len(failures)} case(s) did not pass:"]
            for outcome in failures[:20]:
                lines.append(f"  {outcome.case_id:<28} {outcome.status:<20} {outcome.message[:70]}")
        return "\n".join(lines)


def _fmt(name: str, value: float) -> str:
    if name.endswith("_rate") or name.endswith("_accuracy") or name.endswith("fidelity"):
        return f"{value * 100:.1f}%"
    if "latency" in name or name.endswith("_s"):
        return f"{value:.1f}s"
    if "cost" in name:
        return f"${value:.3f}"
    return f"{value:.2f}"


def run_case(case: BenchmarkCase, orchestrator: Orchestrator) -> CaseOutcome:
    """Run one case and score it."""
    started = time.perf_counter()
    # Clarification cases are the only ones run interactively -- everywhere else
    # a question would stall the benchmark rather than answer it.
    interactive = case.difficulty == "clarify"

    try:
        result = orchestrator.generate(
            case.prompt,
            printer_profile=DEFAULT_PROFILE_ID,
            interactive=interactive,
        )
    except Exception as exc:  # noqa: BLE001
        return CaseOutcome(
            case_id=case.id,
            difficulty=case.difficulty,
            ok=False,
            status="error",
            duration_s=time.perf_counter() - started,
            message=f"{type(exc).__name__}: {exc}",
        )

    duration = time.perf_counter() - started
    report = result.validation or {}
    measurements = report.get("measurements") or {}
    checks = {c["id"]: c for c in report.get("checks", [])}

    outcome = CaseOutcome(
        case_id=case.id,
        difficulty=case.difficulty,
        ok=result.ok,
        status=result.status,
        duration_s=duration,
        iterations=result.iterations,
        template_id=result.template_id,
        route=result.route,
        built=bool(measurements),
        manifold=bool(measurements.get("watertight")),
        dfm_passed=bool(report.get("passed")),
        unavailable="needs a Claude API client" in (result.message or ""),
        overhang_fraction=measurements.get("overhang_fraction"),
        cost_usd=float(result.usage.cost_usd or 0.0),
        message=result.message,
    )

    if case.expects_template:
        outcome.routed_correctly = result.template_id == case.expects_template

    dimension_checks = [c for cid, c in checks.items() if cid.startswith("dimension.")]
    if dimension_checks:
        outcome.dimensions_correct = all(c["passed"] for c in dimension_checks)
    elif case.expects_dimensions:
        outcome.dimensions_correct = _dimensions_match(
            measurements.get("bbox_mm") or [], case.expects_dimensions
        )

    if case.difficulty == "adversarial":
        refused = result.status == "refused"
        outcome.refusal_correct = refused == case.expects_refusal
        # For adversarial cases the score is refusal accuracy, not whether a
        # model came out: a correctly refused request has no geometry and that
        # is the right answer.
        outcome.ok = bool(outcome.refusal_correct)

    if case.difficulty == "clarify":
        asked = result.status == "needs_clarification"
        outcome.clarification_correct = asked == case.expects_clarification
        outcome.ok = bool(outcome.clarification_correct)

    return outcome


def _dimensions_match(bbox: list[float], expected: dict[str, float], tolerance: float = 0.02) -> bool:
    if not bbox or len(bbox) != 3:
        return False
    axes = {"width_mm": 0, "length_mm": 0, "depth_mm": 1, "height_mm": 2}
    for key, target in expected.items():
        index = axes.get(key)
        if index is None:
            continue
        if abs(bbox[index] - target) / target > tolerance:
            return False
    return True


def compute_metrics(outcomes: list[CaseOutcome]) -> dict[str, float]:
    """Aggregate the per-case outcomes into the spec's metric set."""
    attempted = [o for o in outcomes if o.difficulty not in {"adversarial", "clarify"}]
    # Cases blocked by a missing capability are excluded from every rate. They
    # are counted separately, because a run with no API key would otherwise
    # report the freeform cases as geometry failures -- which is exactly the
    # opposite of what happened.
    scorable = [o for o in attempted if not o.unavailable]
    succeeded = [o for o in scorable if o.ok]
    # Geometry rates are measured over cases that produced geometry.
    with_geometry = [o for o in scorable if o.built]
    metrics: dict[str, float] = {}

    def record(name: str, numerator: list, denominator: list) -> None:
        """Record a rate, or nothing at all when there is nothing to measure.

        Reporting 0% for an empty denominator would be a lie that reads exactly
        like a catastrophic regression: run only the adversarial subset and
        every geometry metric shows as a total failure.
        """
        if denominator:
            metrics[name] = round(len(numerator) / len(denominator), 4)

    def rate(numerator: list, denominator: list) -> float:
        return round(len(numerator) / len(denominator), 4) if denominator else 0.0

    record("completion_rate", succeeded, scorable)
    record("manifold_rate", [o for o in with_geometry if o.manifold], with_geometry)
    record("dfm_pass_rate", [o for o in with_geometry if o.dfm_passed], with_geometry)
    record("first_pass_rate", [o for o in succeeded if o.iterations <= 1], succeeded)

    if succeeded:
        metrics["mean_iterations"] = round(
            statistics.mean(o.iterations for o in succeeded), 3
        )

    dimensioned = [o for o in scorable if o.dimensions_correct is not None]
    record(
        "dimensional_fidelity", [o for o in dimensioned if o.dimensions_correct], dimensioned
    )

    routed = [o for o in outcomes if o.routed_correctly is not None]
    record("routing_accuracy", [o for o in routed if o.routed_correctly], routed)
    record("template_hit_rate", [o for o in scorable if o.template_id], scorable)

    support_free = [o for o in succeeded if o.overhang_fraction is not None]
    record(
        "support_free_rate",
        [o for o in support_free if (o.overhang_fraction or 0) < 0.05],
        support_free,
    )

    adversarial = [o for o in outcomes if o.refusal_correct is not None]
    record("refusal_accuracy", [o for o in adversarial if o.refusal_correct], adversarial)

    clarify = [o for o in outcomes if o.clarification_correct is not None]
    record(
        "clarification_accuracy",
        [o for o in clarify if o.clarification_correct],
        clarify,
    )

    if succeeded:
        durations = sorted(o.duration_s for o in succeeded)
        metrics["p50_latency_s"] = round(durations[len(durations) // 2], 2)
        metrics["p95_latency_s"] = round(
            durations[min(len(durations) - 1, int(len(durations) * 0.95))], 2
        )
        total_cost = sum(o.cost_usd for o in outcomes)
        metrics["cost_per_accepted_usd"] = round(total_cost / len(succeeded), 5)

    return metrics


def run(
    selected: list[BenchmarkCase] | None = None,
    *,
    orchestrator: Orchestrator | None = None,
    on_case=None,
) -> BenchmarkReport:
    """Run the benchmark."""
    from datetime import datetime, timezone

    selected = selected if selected is not None else list(ALL_CASES)
    engine = orchestrator or Orchestrator(
        registry=TemplateRegistry.load(strict=False),
        client=build_client(),
        enable_critique=False,
    )

    started = time.perf_counter()
    report = BenchmarkReport(
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        offline=not engine.client.available,
    )
    for case in selected:
        outcome = run_case(case, engine)
        report.outcomes.append(outcome)
        if on_case:
            on_case(outcome)

    report.duration_s = time.perf_counter() - started
    report.metrics = compute_metrics(report.outcomes)
    return report


def compare(current: BenchmarkReport, baseline: dict) -> list[str]:
    """Regressions against a previous run (spec section 13.3)."""
    previous = baseline.get("metrics") or {}
    regressions: list[str] = []
    for name, value in current.metrics.items():
        before = previous.get(name)
        if before is None:
            continue
        delta = value - before
        margin = REGRESSION_MARGIN
        if name in NOISY_METRICS:
            margin = max(margin, abs(before) * NOISE_FRACTION)
        worse = delta > margin if name in LOWER_IS_BETTER else delta < -margin
        if worse:
            regressions.append(
                f"{name}: {_fmt(name, before)} -> {_fmt(name, value)} "
                f"({delta:+.3f}, margin {margin:.3g})"
            )
    return regressions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--difficulty", choices=["easy", "medium", "hard", "adversarial", "clarify"]
    )
    parser.add_argument("--category")
    parser.add_argument("--json", help="write the full report here")
    parser.add_argument("--baseline", help="compare against a previous report and fail on regressions")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    selected = cases(args.difficulty, args.category)
    if not selected:
        print("no cases matched", file=sys.stderr)
        return 1

    def on_case(outcome: CaseOutcome) -> None:
        if args.quiet:
            return
        marker = "ok" if outcome.ok else "!!"
        print(f"  [{marker}] {outcome.case_id:<28} {outcome.status:<20} {outcome.duration_s:>5.1f}s")

    report = run(selected, on_case=on_case)
    print()
    print(report.render())

    if args.json:
        Path(args.json).write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
        print(f"\nreport written to {args.json}")

    exit_code = 0
    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text())
        regressions = compare(report, baseline)
        print()
        if regressions:
            print("REGRESSIONS against the baseline:")
            for line in regressions:
                print(f"  {line}")
            exit_code = 1
        else:
            print("no regressions against the baseline")

    failing = report.failing_targets()
    if failing:
        print()
        print("below target:")
        for name, (value, target) in failing.items():
            print(f"  {name}: {_fmt(name, value)} (target {_fmt(name, target)})")
        exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
