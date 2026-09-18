"""Build every pattern, at its defaults and at the extremes of its ranges.

The catalogue's own test suite, and the counterpart to `check_templates`. The
claim a parameter range makes is the same one a template schema makes: a range
that permits `cell_mm` down to 4 is a promise that a 4 mm cell builds a solid
you can print, and the only way to hold the catalogue to that is to build it.

It is worth running separately from the unit tests because of what it costs and
what it catches. The unit tests build small panels at coarse sampling; this
sweeps 21 patterns across every extreme of every parameter, which is the regime
where a kernel divides by a radius of zero, a cell size smaller than the sample
pitch aliases into noise, or a footprint operation eats the tile. None of those
show up at the defaults.

Run as a module:

    python -m formforge.eval.check_patterns              # defaults only, fast
    python -m formforge.eval.check_patterns --extremes   # the full sweep
    python -m formforge.eval.check_patterns --id dunes --id tpms
    python -m formforge.eval.check_patterns --joints     # every joint style too
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..dfm import DEFAULT_PROFILE_ID
from ..patterns import JOINT_STYLES, PATTERNS, JointSpec, PanelSpec, PatternSpec, build_panel
from ..patterns.field import Bounds, PatternField
from ..patterns.library import build_field_fn

# Small enough to sweep quickly, big enough to need more than one tile on every
# profile -- the seams are the part worth exercising.
SWEEP_WIDTH_MM = 420.0
SWEEP_HEIGHT_MM = 300.0
SWEEP_PITCH_MM = 2.0


@dataclass
class CaseResult:
    """One pattern built with one set of parameters."""

    pattern_id: str
    case: str
    ok: bool
    detail: str = ""
    tiles: int = 0
    duration_s: float = 0.0
    measurements: dict[str, Any] = field(default_factory=dict)

    def line(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        extra = ""
        if self.ok and self.measurements:
            extra = (
                f" std={self.measurements.get('std', 0):.3f}"
                f" span={self.measurements.get('span', 0):.3f}"
            )
        detail = f" -- {self.detail}" if self.detail else ""
        return (
            f"{status} {self.pattern_id} [{self.case}] {self.duration_s:5.2f}s{extra}{detail}"
        )


def check_field(spec: PatternSpec, params: dict[str, Any], case: str) -> CaseResult:
    """Sample a pattern without meshing it.

    Cheap enough to run over every parameter extreme, and it catches the whole
    class of failure that starts in the kernel: a NaN, an infinity, or a field
    so flat that the panel would print as a plate.
    """
    started = time.time()
    bounds = Bounds.sized(SWEEP_WIDTH_MM, SWEEP_HEIGHT_MM)
    try:
        kernel, _, _ = build_field_fn(spec.id, params, bounds, seed=17)
        field_ = PatternField(fn=kernel, bounds=bounds).calibrate()
        heights = field_.sample_grid(bounds, 220, 160)
    except Exception as exc:  # the harness reports failures, it does not raise
        return CaseResult(
            spec.id,
            case,
            False,
            f"{type(exc).__name__}: {exc}",
            duration_s=time.time() - started,
        )

    duration = time.time() - started
    problems = []
    if not np.isfinite(heights).all():
        problems.append("non-finite heights")
    if heights.min() < 0.0 or heights.max() > 1.0:
        problems.append(f"heights outside [0, 1]: {heights.min():.3f}..{heights.max():.3f}")
    span = float(heights.max() - heights.min())
    if span < 0.25:
        # Not a crash, but a panel that would print as a nearly flat plate is
        # not what the parameter range promised.
        problems.append(f"almost flat: uses {span * 100:.0f}% of the relief")

    return CaseResult(
        spec.id,
        case,
        not problems,
        "; ".join(problems),
        duration_s=duration,
        measurements={"std": float(heights.std()), "span": span},
    )


def check_panel(
    spec: PatternSpec,
    out_dir: Path,
    *,
    joint: str = "key",
    profile_id: str = DEFAULT_PROFILE_ID,
) -> CaseResult:
    """Build a real multi-tile panel and check every tile is a printable solid."""
    import trimesh

    started = time.time()
    panel_spec = PanelSpec(
        pattern_id=spec.id,
        width_mm=SWEEP_WIDTH_MM,
        height_mm=SWEEP_HEIGHT_MM,
        seed=17,
        base_mm=5.0,
        relief_mm=min(spec.suggested_relief_mm, 6.0),
        pitch_mm=SWEEP_PITCH_MM,
        joint=JointSpec(style=joint),
        profile_id=profile_id,
        write_3mf=False,
    )
    try:
        result = build_panel(panel_spec, out_dir / f"{spec.id}-{joint}")
    except Exception as exc:  # the harness reports failures, it does not raise
        return CaseResult(
            spec.id,
            f"panel/{joint}",
            False,
            f"{type(exc).__name__}: {exc}",
            duration_s=time.time() - started,
        )

    problems = []
    for tile in result.tiles:
        mesh = trimesh.load(tile.files["stl"])
        if not mesh.is_watertight:
            problems.append(f"{tile.label} is not watertight")
        elif mesh.volume <= 0:
            problems.append(f"{tile.label} has non-positive volume")
    if result.plan.count < 2:
        problems.append("the sweep panel came out as a single tile; no seam was exercised")

    return CaseResult(
        spec.id,
        f"panel/{joint}",
        not problems,
        "; ".join(problems),
        tiles=result.plan.count,
        duration_s=time.time() - started,
    )


def extreme_cases(spec: PatternSpec) -> list[tuple[str, dict[str, Any]]]:
    """One case per parameter extreme, the others left at their defaults.

    One knob at a time rather than every combination: the combinatorial sweep
    is thousands of cases and its failures are hard to attribute, while this
    names the parameter in the case label.
    """
    cases: list[tuple[str, dict[str, Any]]] = [("defaults", {})]
    for param in spec.params:
        if param.kind == "choice":
            cases += [(f"{param.name}={c}", {param.name: c}) for c in (param.choices or ())]
        elif param.kind == "boolean":
            cases += [(f"{param.name}={v}", {param.name: v}) for v in (True, False)]
        else:
            for label, value in (("min", param.minimum), ("max", param.maximum)):
                if value is not None:
                    cases.append((f"{param.name}={label}", {param.name: value}))
    return cases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m formforge.eval.check_patterns",
        description="Build every pattern at its defaults and at its range extremes.",
    )
    parser.add_argument("--id", action="append", help="check only these patterns; repeatable")
    parser.add_argument("--family", help="check only one family")
    parser.add_argument(
        "--extremes",
        action="store_true",
        help="sweep every parameter to both ends of its range",
    )
    parser.add_argument(
        "--joints",
        action="store_true",
        help="also build a panel in every joint style, not just the default",
    )
    parser.add_argument(
        "--no-panels",
        action="store_true",
        help="sample the fields only; skip the meshing pass",
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE_ID)
    parser.add_argument("--quiet", action="store_true", help="print failures only")
    args = parser.parse_args(argv)

    specs = list(PATTERNS.values())
    if args.family:
        specs = [s for s in specs if s.family == args.family]
    if args.id:
        wanted = set(args.id)
        specs = [s for s in specs if s.id in wanted]
    if not specs:
        print("no patterns matched", file=sys.stderr)
        return 1

    joints = sorted(JOINT_STYLES) if args.joints else ["key"]

    results: list[CaseResult] = []
    with tempfile.TemporaryDirectory(prefix="formforge-patterns-") as scratch:
        out_dir = Path(scratch)
        for spec in specs:
            cases = extreme_cases(spec) if args.extremes else [("defaults", {})]
            for case, params in cases:
                result = check_field(spec, params, case)
                results.append(result)
                if not (result.ok and args.quiet):
                    print(result.line())

            if args.no_panels:
                continue
            for joint in joints:
                result = check_panel(spec, out_dir, joint=joint, profile_id=args.profile)
                results.append(result)
                if not (result.ok and args.quiet):
                    print(result.line())

    failed = [r for r in results if not r.ok]
    print(
        f"\n{len(results) - len(failed)}/{len(results)} case(s) passed across "
        f"{len(specs)} pattern(s)"
    )
    for result in failed:
        print(f"  {result.pattern_id} [{result.case}]: {result.detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
