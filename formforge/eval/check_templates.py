"""Execute and validate every registry template. The registry's own test suite.

A template that does not build, or builds something that fails its own declared
invariants, is worse than a missing template: the system will confidently route
traffic to it. This harness runs each template at its defaults and at the
extremes of its declared parameter ranges, because "works at the default" is a
much weaker claim than the schema makes -- a schema that permits a 200 mm
planter is a promise that a 200 mm planter builds.

Run as a module:

    python -m formforge.eval.check_templates            # defaults only, fast
    python -m formforge.eval.check_templates --extremes # full range sweep
    python -m formforge.eval.check_templates --id planter_halfmoon_wall
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from ..dfm import DEFAULT_PROFILE_ID
from ..registry import Template, TemplateRegistry
from ..sandbox import ExecuteRequest, GeometrySandbox
from ..validation import validate


@dataclass
class CaseResult:
    """One template built with one set of parameters."""

    template_id: str
    case: str
    ok: bool
    skipped: bool = False
    detail: str = ""
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    bbox_mm: list[float] | None = None
    duration_s: float = 0.0

    def line(self) -> str:
        if self.skipped:
            return f"SKIP {self.template_id} [{self.case}] -- {self.detail}"
        status = "PASS" if self.ok else "FAIL"
        bbox = (
            f" bbox={self.bbox_mm[0]:.0f}x{self.bbox_mm[1]:.0f}x{self.bbox_mm[2]:.0f}"
            if self.bbox_mm
            else ""
        )
        warn = f" ({len(self.warnings)} warn)" if self.warnings else ""
        head = f"{status} {self.template_id} [{self.case}] {self.duration_s:.1f}s{bbox}{warn}"
        if self.ok:
            return head
        return head + "\n      " + (self.detail or "; ".join(self.failures))


def parameter_cases(template: Template, extremes: bool) -> list[tuple[str, dict[str, Any]]]:
    """The parameter sets to build this template with.

    Defaults always. With `extremes`, each numeric parameter is additionally
    driven to its minimum and its maximum in isolation -- one at a time rather
    than all at once, so a failure names a single parameter instead of an
    unrepresentable combination of all of them.
    """
    defaults = template.defaults()
    cases: list[tuple[str, dict[str, Any]]] = [("defaults", dict(defaults))]
    if not extremes:
        return cases

    for name, spec in template.properties.items():
        if not isinstance(spec, dict):
            continue
        for bound in ("minimum", "maximum"):
            value = spec.get(bound)
            if value is None or value == defaults.get(name):
                continue
            params = dict(defaults)
            params[name] = value
            cases.append((f"{name}={value:g}", params))
        for choice in spec.get("enum") or []:
            if choice == defaults.get(name):
                continue
            params = dict(defaults)
            params[name] = choice
            cases.append((f"{name}={choice}", params))
    return cases


def check_template(
    template: Template,
    sandbox: GeometrySandbox,
    *,
    extremes: bool = False,
    profile_id: str = DEFAULT_PROFILE_ID,
) -> list[CaseResult]:
    """Build and validate one template across its parameter cases."""
    results: list[CaseResult] = []
    for case_name, params in parameter_cases(template, extremes):
        started = time.perf_counter()

        problems = template.validate_params(params)
        if problems:
            # A combination the template already declares invalid is not a
            # failure to build -- the template said so before we tried, which is
            # the system working. Sweeping one parameter to its extreme
            # routinely produces such a combination, and counting those as
            # failures would bury the cases where the geometry genuinely breaks.
            results.append(
                CaseResult(
                    template.id,
                    case_name,
                    ok=True,
                    skipped=True,
                    detail="; ".join(problems),
                    duration_s=time.perf_counter() - started,
                )
            )
            continue

        execution = sandbox.execute(
            ExecuteRequest(
                source=template.render_source(params),
                language=template.language,
                params=params,
                # Hand-authored templates are reviewed by a human, so the
                # magic-number style rule does not apply to them.
                enforce_named_constants=False,
            )
        )
        if not execution.ok:
            results.append(
                CaseResult(
                    template.id,
                    case_name,
                    ok=False,
                    detail=f"{execution.error_class}: {execution.message}",
                    duration_s=time.perf_counter() - started,
                )
            )
            continue

        report = validate(
            execution.artifacts["stl"],
            profile_id=profile_id,
            material=(template.tested.target_material if template.tested else "PLA"),
            category=template.category,
            params=params,
            template_invariants=template.invariants,
            expected_solids=template.expected_solids,
            brep_features=execution.stats.get("brep_features"),
            text_features=template.resolve_text_features(params),
            requested_dimensions=template.requested_dimensions(params),
        )
        results.append(
            CaseResult(
                template.id,
                case_name,
                ok=report.passed,
                detail="" if report.passed else report.agent_feedback(),
                failures=[c.id for c in report.hard_failures],
                warnings=[c.id for c in report.warnings],
                bbox_mm=execution.stats.get("bbox_mm"),
                duration_s=time.perf_counter() - started,
            )
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", action="append", help="check only these template ids")
    parser.add_argument("--category", help="check only this category")
    parser.add_argument(
        "--extremes",
        action="store_true",
        help="also build each parameter at its minimum and maximum",
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE_ID)
    parser.add_argument("--quiet", action="store_true", help="print failures only")
    args = parser.parse_args(argv)

    registry = TemplateRegistry.load(strict=False)
    errors = getattr(registry, "load_errors", [])
    for error in errors:
        print(f"LOAD-FAIL {error}", file=sys.stderr)

    templates = registry.list(args.category)
    if args.id:
        wanted = set(args.id)
        templates = [t for t in templates if t.id in wanted]

    if not templates:
        print("no templates matched", file=sys.stderr)
        return 1

    sandbox = GeometrySandbox()
    all_results: list[CaseResult] = []
    for template in templates:
        results = check_template(
            template, sandbox, extremes=args.extremes, profile_id=args.profile
        )
        all_results.extend(results)
        for result in results:
            if (result.ok or result.skipped) and args.quiet:
                continue
            print(result.line())

    failed = [r for r in all_results if not r.ok]
    skipped = [r for r in all_results if r.skipped]
    built = len(all_results) - len(skipped)
    print(
        f"\n{built - len(failed)}/{built} built cases passed across "
        f"{len(templates)} template(s)"
        + (f"; {len(skipped)} combination(s) rejected up front" if skipped else "")
    )
    if errors:
        print(f"{len(errors)} template file(s) failed to load")
    return 1 if failed or errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
