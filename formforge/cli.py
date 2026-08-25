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
    _add_build(subparsers)
    _add_templates(subparsers)
    _add_check(subparsers)
    _add_render(subparsers)
    _add_slice(subparsers)
    _add_rules(subparsers)
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
            tested = _ok(" [tested]") if match.template.tested else ""
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
        tested = _ok(" [print tested]") if template.tested and template.tested.passed else ""
        print(f"  {template.id:<28} {template.display_name}{tested}")
    return 0


def _print_template(template) -> None:
    print(_c(template.display_name, "1"))
    print(_dim(f"{template.id} v{template.version} -- {template.category}"))
    print()
    print(template.description)
    if template.tested:
        print()
        print(
            f"Print tested: {template.tested.printer}, {template.tested.material}, "
            f"{template.tested.date} ({template.tested.result})"
        )
        if template.tested.notes:
            print(_dim("  " + template.tested.notes.strip().replace("\n", "\n  ")))
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

    print(f"  profiles       {', '.join(sorted(PROFILES))}")
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
