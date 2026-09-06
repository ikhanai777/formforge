"""Command line interface.

    formforge generate "a hex planter for a 4 inch pot"
    formforge build keychain_text_tag --set text=RIVER --set body_l_mm=70
    formforge templates --category planter
    formforge check model.stl --profile bambu_p1s_0.4 --category planter
    formforge render model.stl --out previews/
    formforge doctor

The generate command streams the loop as it happens rather than printing a
spinner. That is not decoration: seeing "checking wall thickness... found
1.08 mm at the drainage boss" is how a user learns that the thing doing the
work is measuring rather than guessing, and it is the same event stream the
web app and the API surface (spec section 12).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .dfm import DEFAULT_PROFILE_ID, PROFILES, rules_block
from .llm import build_client
from .orchestrator import Orchestrator
from .registry import TemplateRegistry
from .render import STANDARD_VIEWS, render_views
from .slicer import available as slicer_available, slice_model
from .validation import validate

# Terminal colour, off when not a tty so piped output stays clean.
_TTY = sys.stdout.isatty()

# Repeated here rather than imported from `formforge.keyholder`, which pulls in
# shapely and numpy: building the argument parser happens on every invocation,
# including `formforge --version`. A test pins these against the module's own
# constants so the two cannot drift.
KEYHOLDER_MOUNTS = ("keyhole", "screw", "none")
KEYHOLDER_DETAIL = ("engrave", "cut", "ignore")


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def _ok(text: str) -> str:
    return _c(text, "32")


def _bad(text: str) -> str:
    return _c(text, "31")


def _warn(text: str) -> str:
    return _c(text, "33")


def _dim(text: str) -> str:
    return _c(text, "2")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="formforge",
        description="Turn descriptions into print-ready 3D models via parametric CAD.",
    )
    parser.add_argument("--version", action="version", version=f"formforge {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    _add_generate(subparsers)
    _add_keyholder(subparsers)
    _add_build(subparsers)
    _add_templates(subparsers)
    _add_check(subparsers)
    _add_render(subparsers)
    _add_slice(subparsers)
    _add_rules(subparsers)
    _add_stats(subparsers)
    _add_feedback(subparsers)
    _add_serve(subparsers)
    _add_doctor(subparsers)

    args = parser.parse_args(argv)
    return args.handler(args)


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


def _add_generate(subparsers) -> None:
    parser = subparsers.add_parser(
        "generate", help="generate a model from a natural-language description"
    )
    parser.add_argument("prompt")
    parser.add_argument("--profile", default=DEFAULT_PROFILE_ID, help="printer profile id")
    parser.add_argument("--material", default="PLA")
    parser.add_argument("--out", default="out", help="directory for the bundle")
    parser.add_argument(
        "--no-clarify",
        action="store_true",
        help="never ask a clarifying question; assume defaults and document them",
    )
    parser.add_argument("--no-critique", action="store_true", help="skip the visual critique")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument(
        "--no-store",
        action="store_true",
        help="do not record this run in the local database",
    )
    parser.set_defaults(handler=_cmd_generate)


def _cmd_generate(args) -> int:
    from .bundle import write_bundle  # noqa: PLC0415

    out_dir = Path(args.out)
    orchestrator = Orchestrator(
        output_dir=out_dir,
        enable_critique=not args.no_critique,
    )

    if not args.json:
        print(_dim(f"prompt: {args.prompt}"))
        print()

    def on_event(event) -> None:
        if args.json:
            return
        marker = _ok("ok") if event.ok else _bad("!!")
        print(f"  [{marker}] {event.phase:<9} {event.message}")

    result = orchestrator.generate(
        args.prompt,
        printer_profile=args.profile,
        material=args.material,
        interactive=not args.no_clarify,
        on_event=on_event,
    )

    if result.status == "ok":
        template = (
            orchestrator.registry.get(result.template_id)
            if result.template_id and result.template_id in orchestrator.registry
            else None
        )
        bundle = write_bundle(result, out_dir / result.model_id / "bundle", template=template)
        result.artifacts.update(bundle.files)

    # Recorded for every terminal status. The two tables this fills cannot be
    # backfilled, and a run on someone's laptop is as much evidence as a run in
    # production -- more, early on, because that is where the runs are.
    if not args.no_store:
        from .store import Store  # noqa: PLC0415

        with Store() as database:
            database.record_generation(result)

    if args.json:
        print(json.dumps(result.as_dict(), indent=2, default=str))
        return 0 if result.ok else 1

    print()
    print(result.summary())
    if result.status == "ok":
        print()
        print(f"bundle: {out_dir / result.model_id / 'bundle'}")
        _print_warnings(result.validation)
    elif result.status == "needs_clarification":
        return 2
    return 0 if result.ok else 1


def _print_warnings(report: dict | None) -> None:
    for warning in (report or {}).get("warnings", [])[:5]:
        print(_warn(f"  warning: {warning.get('message', '')}"))


# ---------------------------------------------------------------------------
# keyholder (image in, wall-mounted key rack out)
# ---------------------------------------------------------------------------


def _add_keyholder(subparsers) -> None:
    parser = subparsers.add_parser(
        "keyholder",
        help="turn an image into a wall-mounted key holder",
        description=(
            "Trace a picture into a silhouette, put a hook rail under it and "
            "keyhole hangers in it, and build the result as a printable solid. "
            "Cut-outs and clip art trace cleanly; photographs depend on their "
            "background, so start with --plan-only and look at the drawing."
        ),
    )
    # Every option defaults to None so that "not given" reaches KeyHolderSpec as
    # "not given". Repeating the numbers here would put a second set of defaults
    # in the system, and the two would eventually disagree.
    parser.add_argument("image", help="PNG, JPEG or WebP. Transparency is used when present.")
    parser.add_argument("--width", type=float, help="overall width in mm (default 180)")
    parser.add_argument(
        "--hooks", type=int, help="how many hooks; 0 picks a count from the width"
    )
    parser.add_argument("--plaque-t", type=float, help="plate thickness in mm (default 7)")
    parser.add_argument(
        "--mount",
        choices=KEYHOLDER_MOUNTS,
        help="keyhole slots (default), countersunk screws, or nothing",
    )
    parser.add_argument(
        "--detail",
        choices=KEYHOLDER_DETAIL,
        help="what to do with openings inside the outline (default engrave)",
    )
    parser.add_argument(
        "--min-feature",
        type=float,
        help="anything thinner than this is removed before it is built (default 2.4 mm)",
    )
    parser.add_argument(
        "--border", type=float, help="grow the silhouette by this much, in mm"
    )
    parser.add_argument(
        "--hook-out", type=float, help="how far a hook projects (default 20 mm)"
    )
    parser.add_argument("--rail-h", type=float, help="hook rail height in mm (default 18)")
    parser.add_argument(
        "--rail-overlap",
        type=float,
        help="how far the rail reaches up into the silhouette (default 4 mm)",
    )
    parser.add_argument("--no-rail", action="store_true", help="silhouette only, no hooks")
    parser.add_argument(
        "--threshold",
        type=float,
        help="override the automatic foreground cut, 0..1",
    )
    parser.add_argument(
        "--invert", action="store_true", help="the subject is the region the frame touches"
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE_ID)
    parser.add_argument("--material", default="PLA")
    parser.add_argument("--out", default="out")
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="trace and lay out, draw the outline, and stop before the CAD kernel",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-store", action="store_true")
    parser.set_defaults(handler=_cmd_keyholder)


def _keyholder_module():
    from . import keyholder  # noqa: PLC0415

    return keyholder


def _keyholder_spec(args):
    keyholder = _keyholder_module()
    chosen = {
        "width_mm": args.width,
        "plaque_t_mm": args.plaque_t,
        "border_mm": args.border,
        "min_feature_mm": args.min_feature,
        "detail": args.detail,
        "rail_h_mm": args.rail_h,
        "rail_overlap_mm": args.rail_overlap,
        "hook_count": args.hooks,
        "hook_out_mm": args.hook_out,
        "mount": args.mount,
        "profile_id": args.profile,
        "material": args.material,
    }
    settings = {key: value for key, value in chosen.items() if value is not None}
    if args.no_rail:
        settings["rail"] = False
        settings["hook_count"] = 0
    return keyholder.KeyHolderSpec(**settings)


def _cmd_keyholder(args) -> int:
    from .bundle import write_bundle  # noqa: PLC0415

    keyholder = _keyholder_module()
    image = Path(args.image)
    if not image.exists():
        print(_bad(f"no such image: {image}"), file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    try:
        spec = _keyholder_spec(args)
    except Exception as exc:  # noqa: BLE001
        print(_bad(str(exc)), file=sys.stderr)
        return 1

    if args.plan_only:
        return _keyholder_plan_only(args, spec, image, out_dir)

    if not args.json:
        print(_dim(f"image: {image}"))
        print()

    try:
        outcome = keyholder.build_key_holder(
            image,
            spec,
            out_dir=out_dir,
            threshold=args.threshold,
            invert=True if args.invert else None,
            source_name=image.name,
            on_event=None
            if args.json
            else lambda e: print(
                f"  [{_ok('ok') if e.ok else _bad('!!')}] {e.phase:<9} {e.message}"
            ),
        )
    except (keyholder.ImageError, keyholder.TraceError, keyholder.DesignError) as exc:
        print(_bad(str(exc)), file=sys.stderr)
        return 1

    result = outcome.result
    if result.status == "ok":
        bundle = write_bundle(result, out_dir / result.model_id / "bundle")
        result.artifacts.update(bundle.files)

    if not args.no_store:
        from .store import Store  # noqa: PLC0415

        with Store() as database:
            database.record_generation(result)

    if args.json:
        print(json.dumps(outcome.as_dict(), indent=2, default=str))
        return 0 if outcome.ok else 1

    print()
    print(outcome.summary() if outcome.ok else result.summary())
    _print_keyholder_notes(outcome)
    if outcome.ok:
        print()
        print(f"bundle: {out_dir / result.model_id / 'bundle'}")
        _print_warnings(result.validation)
    return 0 if outcome.ok else 1


def _keyholder_plan_only(args, spec, image: Path, out_dir: Path) -> int:
    keyholder = _keyholder_module()
    from .keyholder.preview import render_trace  # noqa: PLC0415

    try:
        plan, mask_info, trace_stats = keyholder.plan_from_image(
            image,
            spec,
            threshold=args.threshold,
            invert=True if args.invert else None,
        )
    except (keyholder.ImageError, keyholder.TraceError, keyholder.DesignError) as exc:
        print(_bad(str(exc)), file=sys.stderr)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    drawing = render_trace(plan, out_dir / f"{image.stem}_trace.png")

    if args.json:
        print(
            json.dumps(
                {
                    "plan": plan.as_dict(),
                    "image": mask_info.as_dict(),
                    "trace": trace_stats.as_dict(),
                    "drawing": str(drawing),
                },
                indent=2,
            )
        )
        return 0

    print(plan.summary())
    for note in mask_info.notes:
        print(_dim(f"  image: {note}"))
    for note in trace_stats.notes:
        print(_dim(f"  trace: {note}"))
    for note in plan.notes:
        print(_dim(f"  plan:  {note}"))
    for warning in plan.warnings:
        print(_warn(f"  warning: {warning}"))
    print()
    print(f"drawing: {drawing}")
    return 0


def _print_keyholder_notes(outcome) -> None:
    for note in (outcome.mask_info.notes if outcome.mask_info else []):
        print(_dim(f"  image: {note}"))
    for note in (outcome.plan.notes if outcome.plan else []):
        print(_dim(f"  plan:  {note}"))
    for warning in (outcome.plan.warnings if outcome.plan else []):
        print(_warn(f"  warning: {warning}"))
    if outcome.result.previews.get("trace"):
        print(_dim(f"  traced outline: {outcome.result.previews['trace']}"))


# ---------------------------------------------------------------------------
# build (template, explicit parameters)
# ---------------------------------------------------------------------------


def _add_build(subparsers) -> None:
    parser = subparsers.add_parser(
        "build", help="build a specific template with explicit parameters"
    )
    parser.add_argument("template_id")
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="set a parameter; repeatable",
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE_ID)
    parser.add_argument("--material", default="PLA")
    parser.add_argument("--out", default="out")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=_cmd_build)


def _cmd_build(args) -> int:
    from .bundle import write_bundle  # noqa: PLC0415

    registry = TemplateRegistry.load(strict=False)
    try:
        template = registry.get(args.template_id)
    except KeyError as exc:
        print(_bad(str(exc)), file=sys.stderr)
        return 1

    params = _parse_settings(args.set, template)
    problems = template.validate_params(template.merge_params(params))
    if problems:
        print(_bad("these parameters are outside the template's tested range:"), file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    orchestrator = Orchestrator(registry=registry, output_dir=out_dir, enable_critique=False)
    result = orchestrator.generate(
        prompt=f"{template.display_name} with explicit parameters",
        printer_profile=args.profile,
        material=args.material,
        interactive=False,
        template_id=args.template_id,
        params=params,
        on_event=None if args.json else lambda e: print(
            f"  [{_ok('ok') if e.ok else _bad('!!')}] {e.phase:<9} {e.message}"
        ),
    )

    if result.status == "ok":
        bundle = write_bundle(result, out_dir / result.model_id / "bundle", template=template)
        result.artifacts.update(bundle.files)

    if args.json:
        print(json.dumps(result.as_dict(), indent=2, default=str))
        return 0 if result.ok else 1

    print()
    print(result.summary())
    if result.ok:
        print(f"bundle: {out_dir / result.model_id / 'bundle'}")
        _print_warnings(result.validation)
    return 0 if result.ok else 1


def _parse_settings(settings: list[str], template) -> dict[str, Any]:
    """Parse `--set key=value`, coercing to the schema's declared type.

    Coercion matters: the schema says `body_l_mm` is a number, and a string "70"
    would bind as a string literal into the script and produce a TypeError deep
    inside the kernel rather than a clear message here.
    """
    params: dict[str, Any] = {}
    for setting in settings:
        if "=" not in setting:
            raise SystemExit(f"--set expects KEY=VALUE, got {setting!r}")
        key, _, raw = setting.partition("=")
        key = key.strip()
        spec = template.properties.get(key)
        if spec is None:
            known = ", ".join(sorted(template.properties))
            raise SystemExit(f"{template.id} has no parameter {key!r}. Known: {known}")
        params[key] = _coerce(raw.strip(), spec)
    return params


def _coerce(raw: str, spec: dict) -> Any:
    declared = spec.get("type")
    types = declared if isinstance(declared, list) else [declared]
    if "boolean" in types:
        return raw.lower() in {"1", "true", "yes", "on"}
    if "integer" in types:
        try:
            return int(float(raw))
        except ValueError:
            raise SystemExit(f"expected an integer, got {raw!r}") from None
    if "number" in types:
        try:
            return float(raw)
        except ValueError:
            raise SystemExit(f"expected a number, got {raw!r}") from None
    return raw


# ---------------------------------------------------------------------------
# templates
# ---------------------------------------------------------------------------


def _add_templates(subparsers) -> None:
    parser = subparsers.add_parser("templates", help="list or inspect templates")
    parser.add_argument("template_id", nargs="?", help="show one template in detail")
    parser.add_argument("--category")
    parser.add_argument("--search", help="free-text search")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=_cmd_templates)


def _cmd_templates(args) -> int:
    registry = TemplateRegistry.load(strict=False)
    for error in getattr(registry, "load_errors", []):
        print(_bad(f"failed to load: {error}"), file=sys.stderr)

    if args.template_id:
        try:
            template = registry.get(args.template_id)
        except KeyError as exc:
            print(_bad(str(exc)), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(template.detail(), indent=2))
            return 0
        _print_template(template)
        return 0

    if args.search:
        matches = registry.search(args.search, args.category, limit=10)
        if args.json:
            print(json.dumps([m.as_dict() for m in matches], indent=2))
            return 0
        print(f"{len(matches)} match(es) for {args.search!r}:\n")
        for match in matches:
            tested = (
                _ok(" [print tested]")
                if match.template.tested and match.template.tested.passed
                else ""
            )
            print(
                f"  {match.score:>5.2f}  {match.template.id:<28} "
                f"{match.template.display_name}{tested}"
            )
            print(_dim(f"         route: {match.route.value}"))
        return 0

    templates = registry.list(args.category)
    if args.json:
        print(json.dumps([t.summary() for t in templates], indent=2))
        return 0

    print(f"{len(templates)} template(s)\n")
    category = None
    for template in templates:
        if template.category != category:
            category = template.category
            print(_c(category.replace("_", " ").upper(), "1"))
        badge = _ok(" [print tested]") if template.tested and template.tested.passed else ""
        print(f"  {template.id:<28} {template.display_name}{badge}")
    return 0


def _print_template(template) -> None:
    print(_c(template.display_name, "1"))
    print(_dim(f"{template.id} v{template.version} -- {template.category}"))
    print()
    print(template.description)
    if template.tested:
        print()
        if template.tested.passed:
            print(
                _ok(f"Print tested: {template.tested.target_printer}, "
                    f"{template.tested.target_material}, {template.tested.date}")
            )
        else:
            print(
                _warn(f"Not physically printed ({template.tested.status}). Designed for "
                      f"{template.tested.target_printer or 'a generic FDM printer'}, "
                      f"{template.tested.target_material}.")
            )
        if template.tested.rationale:
            print(_dim("  " + template.tested.rationale.strip().replace("\n", "\n  ")))
    print()
    print(_c("Parameters", "1"))
    for name, spec in template.properties.items():
        if not isinstance(spec, dict):
            continue
        default = spec.get("default")
        bounds = ""
        if "minimum" in spec or "maximum" in spec:
            bounds = f" [{spec.get('minimum', '')}..{spec.get('maximum', '')}]"
        elif spec.get("enum"):
            bounds = f" {{{', '.join(str(v) for v in spec['enum'])}}}"
        required = _c("*", "31") if name in template.required else " "
        print(f" {required}{name:<20} = {default!r:<12}{bounds}")
        if spec.get("description"):
            print(_dim(f"    {spec['description'].strip()}"))
    if template.invariants:
        print()
        print(_c("Guarantees", "1"))
        for invariant in template.invariants:
            print(f"  {invariant}")


# ---------------------------------------------------------------------------
# check / render / slice / rules / doctor
# ---------------------------------------------------------------------------


def _add_check(subparsers) -> None:
    parser = subparsers.add_parser("check", help="run the DFM suite on an existing mesh")
    parser.add_argument("mesh")
    parser.add_argument("--profile", default=DEFAULT_PROFILE_ID)
    parser.add_argument("--material", default="PLA")
    parser.add_argument("--category")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=_cmd_check)


def _cmd_check(args) -> int:
    report = validate(
        args.mesh,
        profile_id=args.profile,
        material=args.material,
        category=args.category,
    )
    if args.json:
        print(report.to_json())
        return 0 if report.passed else 1

    print(_ok(report.summary_line()) if report.passed else _bad(report.summary_line()))
    print()
    print(report.agent_feedback())
    return 0 if report.passed else 1


def _add_render(subparsers) -> None:
    parser = subparsers.add_parser("render", help="render preview images of a mesh")
    parser.add_argument("mesh")
    parser.add_argument("--out", default="previews")
    parser.add_argument("--size", type=int, default=512)
    parser.add_argument(
        "--views",
        default="iso,front,top,section",
        help=f"comma-separated; available: {','.join(STANDARD_VIEWS)}",
    )
    parser.set_defaults(handler=_cmd_render)


def _cmd_render(args) -> int:
    views = tuple(v.strip() for v in args.views.split(",") if v.strip())
    result = render_views(args.mesh, args.out, views=views, size=args.size)
    for name, path in result.views.items():
        print(f"  {name:<10} {path}")
    if result.contact_sheet:
        print(f"  {'sheet':<10} {result.contact_sheet}")
    return 0


def _add_slice(subparsers) -> None:
    parser = subparsers.add_parser("slice", help="slice a model for print estimates")
    parser.add_argument("mesh")
    parser.add_argument("--profile", default=DEFAULT_PROFILE_ID)
    parser.add_argument("--quality", default="standard", choices=["draft", "standard", "fine"])
    parser.add_argument("--supports", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=_cmd_slice)


def _cmd_slice(args) -> int:
    summary = slice_model(
        args.mesh,
        profile_id=args.profile,
        quality=args.quality,
        supports=args.supports,
    )
    if args.json:
        print(json.dumps(summary.as_dict(), indent=2))
        return 0 if summary.ok else 1
    if not summary.available:
        print(_warn(summary.error))
        return 1
    if not summary.ok:
        print(_bad(summary.error))
        return 1
    print(f"  print time   {summary.print_time_human}")
    print(f"  filament     {summary.filament_g or 0:.1f} g ({summary.filament_mm or 0:.0f} mm)")
    print(f"  layers       {summary.layer_count}")
    if summary.support_ratio is not None:
        print(f"  support      {summary.support_ratio * 100:.1f}% of part volume")
    feedback = summary.agent_feedback()
    if feedback:
        print()
        print(_warn(feedback))
    return 0


def _add_rules(subparsers) -> None:
    parser = subparsers.add_parser(
        "rules", help="print the DFM rules for a printer and material"
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE_ID)
    parser.add_argument("--material", default="PLA")
    parser.set_defaults(handler=lambda a: (print(rules_block(a.profile, a.material)), 0)[1])


# ---------------------------------------------------------------------------
# stats and feedback -- what the collected data is for
# ---------------------------------------------------------------------------


def _add_stats(subparsers) -> None:
    parser = subparsers.add_parser(
        "stats", help="what the recorded generations say about the system"
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--db", help="database path (default: $FORMFORGE_DB)")
    parser.set_defaults(handler=_cmd_stats)


def _cmd_stats(args) -> int:
    """Three questions no amount of validation can answer.

    Which templates are quietly failing, which errors actually dominate, and
    whether any of it prints. All three need history, and history has to have
    been collected at the time.
    """
    from .store import Store  # noqa: PLC0415

    with Store(args.db) as database:
        totals = database.totals()
        health = database.template_health()
        failures = database.failure_classes()
        prints = database.print_outcomes()

    if args.json:
        print(json.dumps(
            {"totals": totals, "templates": health, "failures": failures,
             "prints": prints},
            indent=2, default=str,
        ))
        return 0

    if not totals["generations"]:
        print(_dim("No generations recorded yet."))
        print(_dim("Run `formforge generate ...` -- every run is recorded."))
        return 0

    rate = totals["succeeded"] / totals["generations"]
    print(_c("Generations", "1"))
    print(f"  recorded       {totals['generations']}")
    print(f"  succeeded      {totals['succeeded']} ({rate:.0%})")
    print(f"  refused        {totals['refused']}")
    print(f"  mean iterations{totals['mean_iterations']:>6.2f}")
    print(f"  cost           ${totals['cost_usd']:.4f}")
    if totals["write_failures"]:
        print(_bad(f"  write failures {totals['write_failures']} -- telemetry is being lost"))

    if health:
        print()
        print(_c("Templates", "1"))
        print(f"  {'template':<28}{'runs':>6}{'ok':>7}{'iters':>7}{'prints':>8}")
        for row in health[:15]:
            success = row["success_rate"] or 0.0
            label = f"  {row['template_id']:<28}{row['generations']:>6}"
            label += f"{success:>6.0%} {row['mean_iterations'] or 0:>6.2f}"
            label += f"{row['prints_reported'] or 0:>8}"
            print(label if success >= 0.9 else _warn(label))

    if failures:
        print()
        print(_c("Failure classes", "1"))
        for row in failures:
            print(f"  {row['error_class']:<34}{row['occurrences']:>5}   {row['last_seen']}")

    print()
    if prints:
        reported = len(prints)
        worked = sum(1 for p in prints if p["success"])
        print(_c("Prints reported", "1") + f"  {worked}/{reported} succeeded")
        issues: dict[str, int] = {}
        for outcome in prints:
            for issue in outcome["issues"]:
                issues[issue] = issues.get(issue, 0) + 1
        for issue, count in sorted(issues.items(), key=lambda kv: -kv[1]):
            print(f"  {issue:<34}{count:>5}")
    else:
        # Stated rather than left blank: an empty table here is the difference
        # between DFM constants that are measured and DFM constants that are
        # conventional, and that difference should be visible.
        print(_warn("No print outcomes reported yet."))
        print(_dim("Until this has rows, every DFM constant is a maker convention,"))
        print(_dim("not a measurement. `formforge feedback <model-id> ...` adds one."))
    return 0


def _add_feedback(subparsers) -> None:
    parser = subparsers.add_parser(
        "feedback", help="record what happened when a model was printed"
    )
    parser.add_argument("model_id")
    parser.add_argument(
        "--failed",
        action="store_true",
        help="the print did not come out usable (default: it did)",
    )
    parser.add_argument(
        "--issue",
        action="append",
        default=[],
        metavar="NAME",
        help="what went wrong; repeatable",
    )
    parser.add_argument("--printer")
    parser.add_argument("--material")
    parser.add_argument("--notes")
    parser.add_argument("--db", help="database path (default: $FORMFORGE_DB)")
    parser.set_defaults(handler=_cmd_feedback)


def _cmd_feedback(args) -> int:
    from .store import PRINT_ISSUES, Store  # noqa: PLC0415

    unknown = sorted(set(args.issue) - PRINT_ISSUES)
    if unknown:
        print(_bad(f"unknown issue(s): {', '.join(unknown)}"))
        print(_dim("expected: " + ", ".join(sorted(PRINT_ISSUES))))
        return 2

    with Store(args.db) as database:
        if database.get_model(args.model_id) is None:
            print(_bad(f"no model {args.model_id} in the database"))
            print(_dim("feedback has to point at a recorded generation, so it can"))
            print(_dim("be read back against what the validator measured."))
            return 1
        feedback_id = database.record_feedback(
            {
                "model_id": args.model_id,
                "printed": True,
                "success": not args.failed,
                "printer": args.printer,
                "material": args.material,
                "issues": args.issue,
                "notes": args.notes,
            }
        )
    print(_ok("recorded") + f" {feedback_id}")
    return 0


def _add_serve(subparsers) -> None:
    parser = subparsers.add_parser(
        "serve",
        help="run the HTTP API and the browser interface",
        description=(
            "Serves the key holder interface at / and the REST API under /v1. "
            "Binds to the loopback address: this executes generated Python, so "
            "putting it on a network needs a sandbox runtime that isolates the "
            "host kernel."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--allow-unsafe-sandbox",
        action="store_true",
        help=(
            "start even though the sandbox runtime does not isolate the host "
            "kernel. Loopback only, and never in production."
        ),
    )
    parser.set_defaults(handler=_cmd_serve)


# Addresses that reach only this machine. The unsafe-sandbox override is
# accepted on these and refused everywhere else -- "I accept the risk on my own
# laptop" and "I accept it for the internet" are different sentences, and the
# env var alone cannot tell them apart.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"})


def _cmd_serve(args) -> int:
    try:
        import uvicorn  # noqa: PLC0415
    except ImportError:
        print(
            _bad("the HTTP server needs FastAPI and uvicorn: "
                 "pip install 'formforge[api]'"),
            file=sys.stderr,
        )
        return 1

    from .api import create_app  # noqa: PLC0415
    from .sandbox import GeometrySandbox  # noqa: PLC0415

    isolated = GeometrySandbox().production_ready()
    loopback = args.host in LOOPBACK_HOSTS
    if not isolated and args.allow_unsafe_sandbox and not loopback:
        print(
            _bad(
                f"--allow-unsafe-sandbox is refused on {args.host}: this server "
                "executes generated Python, and the runtime does not isolate "
                "the host kernel. Bind to 127.0.0.1, or set "
                "FORMFORGE_SANDBOX_RUNTIME=gvisor."
            ),
            file=sys.stderr,
        )
        return 1

    try:
        app = create_app(allow_unsafe_sandbox=args.allow_unsafe_sandbox)
    except RuntimeError as exc:
        print(_bad(str(exc)), file=sys.stderr)
        if not isolated:
            print(
                _warn(
                    "\nFor a local server on your own machine, this is the flag "
                    "that says you accept that:\n"
                    f"  formforge serve --allow-unsafe-sandbox --port {args.port}"
                ),
                file=sys.stderr,
            )
        return 1

    if not isolated:
        print(
            _warn(
                "sandbox: no kernel isolation. Local use only -- do not put this "
                "on a network."
            )
        )
    print(f"key holder interface: {_c(f'http://{args.host}:{args.port}/', '1')}")
    print(_dim(f"api docs: http://{args.host}:{args.port}/docs"))
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def _add_doctor(subparsers) -> None:
    parser = subparsers.add_parser(
        "doctor", help="report what is installed, configured and safe"
    )
    parser.set_defaults(handler=_cmd_doctor)


def _cmd_doctor(args) -> int:
    """Report the environment, loudly where it matters.

    The sandbox line is the important one. Running the development runtime in
    production means executing model-authored Python with no kernel isolation,
    and that is the single most consequential misconfiguration this system has.
    """
    from .sandbox import GeometrySandbox  # noqa: PLC0415

    print(_c("FormForge", "1") + f" {__version__}")
    print()

    registry = TemplateRegistry.load(strict=False)
    errors = getattr(registry, "load_errors", [])
    print(f"  templates      {len(registry)} loaded across {len(registry.categories())} categories")
    for error in errors:
        print(_bad(f"                 failed: {error}"))

    sandbox = GeometrySandbox()
    described = sandbox.describe()
    if described["kernel_isolated"]:
        print(f"  sandbox        {_ok(described['runtime'])} (kernel isolated)")
    else:
        print(f"  sandbox        {_warn(described['runtime'])} -- {described['warning']}")

    client = build_client()
    if client.available:
        print(f"  claude api     {_ok('configured')}")
    else:
        print(
            f"  claude api     {_warn('not configured')} -- template path only; "
            "no intent parsing, freeform generation or visual critique"
        )

    print(f"  slicer         {_ok('found') if slicer_available() else _warn('not installed')}")

    try:
        import rtree  # noqa: F401, PLC0415

        print(f"  wall thickness {_ok('accelerated')}")
    except ImportError:
        print(f"  wall thickness {_warn('unaccelerated')} -- install rtree for full resolution")

    try:
        import PIL  # noqa: F401, PLC0415

        print(f"  image tracing  {_ok('available')} -- formforge keyholder")
    except ImportError:
        print(
            f"  image tracing  {_warn('unavailable')} -- install "
            "\"formforge[image]\" for `formforge keyholder`"
        )

    print(f"  profiles       {', '.join(sorted(PROFILES))}")

    from .store import DEFAULT_PATH, Store  # noqa: PLC0415

    with Store() as database:
        totals = database.totals()
    print(f"  database       {DEFAULT_PATH}")
    print(
        f"                 {totals['generations']} generation(s), "
        f"{totals['prints_reported']} print outcome(s) reported"
    )
    if not totals["prints_reported"]:
        # The honest state of the DFM constants, stated where someone checking
        # their setup will read it.
        print(
            _warn("                 no prints reported -- every DFM threshold "
                  "here is a convention, not a measurement")
        )
    print()

    if not described["kernel_isolated"]:
        print(
            _warn(
                "The geometry sandbox does not isolate the host kernel. That is "
                "fine locally; it must not serve untrusted input. Set "
                "FORMFORGE_SANDBOX_RUNTIME=gvisor in production."
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
