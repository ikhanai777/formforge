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
from .llm import Tier, build_client
from .orchestrator import Orchestrator
from .registry import TemplateRegistry
from .render import STANDARD_VIEWS, render_views
from .slicer import available as slicer_available
from .slicer import slice_model
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
    _add_emboss(subparsers)
    _add_reconstruct(subparsers)
    _add_models(subparsers)
    _add_check(subparsers)
    _add_render(subparsers)
    _add_slice(subparsers)
    _add_rules(subparsers)
    _add_stats(subparsers)
    _add_feedback(subparsers)
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
    from .bundle import write_bundle

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
        from .store import Store

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
    from .bundle import write_bundle

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
# emboss
# ---------------------------------------------------------------------------


def _add_emboss(subparsers) -> None:
    parser = subparsers.add_parser(
        "emboss", help="trace an image into a printable relief"
    )
    parser.add_argument("image")
    parser.add_argument("--out", default="out")
    parser.add_argument("--width", type=float, default=150.0, help="panel width in mm")
    parser.add_argument("--relief", type=float, default=2.8)
    parser.add_argument("--panel", type=float, default=4.0, help="panel thickness in mm")
    parser.add_argument("--margin", type=float, default=10.0)
    parser.add_argument(
        "--standalone",
        action="store_true",
        help="cut the silhouette out on its own instead of raising it on a panel",
    )
    parser.add_argument("--smooth", type=float, default=1.8, help="mask blur, pixels")
    parser.add_argument("--simplify", type=float, default=0.9, help="contour tolerance, pixels")
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--invert", action="store_true")
    parser.add_argument(
        "--fill",
        action="store_true",
        help="outline only; discard interior holes, which on a photograph are "
        "the subject's own trim and highlights rather than design",
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE_ID)
    parser.add_argument("--material", default="PLA")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=_cmd_emboss)


def _cmd_emboss(args) -> int:
    from .emboss import EmbossOptions, emboss_source, load_mask, trace_polygons
    from .sandbox import ExecuteRequest, GeometrySandbox
    from .validation import validate

    image = Path(args.image)
    if not image.exists():
        print(_bad(f"no such image: {image}"), file=sys.stderr)
        return 1

    opts = EmbossOptions(
        width_mm=args.width,
        relief_mm=args.relief,
        panel_t_mm=args.panel,
        margin_mm=args.margin,
        standalone=args.standalone,
        smooth_px=args.smooth,
        simplify_px=args.simplify,
        threshold=args.threshold,
        invert=args.invert,
        fill_holes=args.fill,
    )

    def say(phase: str, ok: bool, message: str) -> None:
        if not args.json:
            print(f"  [{_ok('ok') if ok else _bad('!!')}] {phase:<9} {message}")

    mask = load_mask(image, opts)
    coverage = float(mask.mean())
    say("trace", True, f"subject covers {coverage:.0%} of the frame")
    if coverage > 0.92:
        print(
            _bad(
                "the silhouette is nearly the whole frame, which usually means the "
                "background was not separated. Try --invert or --threshold."
            ),
            file=sys.stderr,
        )
        return 1

    trace = trace_polygons(mask, opts)
    if not trace.polygons:
        print(_bad("; ".join(trace.notes) or "nothing to trace"), file=sys.stderr)
        return 1
    say(
        "contour",
        True,
        f"{len(trace.polygons)} shape(s), {trace.holes} hole(s), {trace.point_count} points",
    )
    for note in trace.notes:
        say("note", True, note)

    if args.standalone and len(trace.polygons) > 1:
        print(
            _bad(
                f"--standalone needs one connected shape; this image traced "
                f"{len(trace.polygons)}. Without a panel they would print as "
                f"loose pieces."
            ),
            file=sys.stderr,
        )
        return 1

    source = emboss_source(trace, opts, image.name)
    sandbox = GeometrySandbox(keep_workdir=True)
    # The contour is data, not a model's magic numbers, so the named-constant
    # rule does not apply to it. Everything that governs printability is
    # already a named constant at the top of the emitted script.
    result = sandbox.execute(ExecuteRequest(source=source, enforce_named_constants=False))
    if not result.ok:
        print(_bad(result.feedback()), file=sys.stderr)
        return 1
    stats = result.stats or {}
    say("execute", True, f"solid built: {stats.get('bbox_mm')}, {stats.get('triangles')} triangles")

    out_dir = Path(args.out) / image.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}
    for key, path in (result.artifacts or {}).items():
        src = Path(path)
        if src.exists():
            dest = out_dir / f"model{src.suffix}"
            dest.write_bytes(src.read_bytes())
            written[key] = dest
    (out_dir / "source.py").write_text(source)

    mesh = written.get("stl") or next(iter(written.values()), None)
    report = validate(str(mesh), profile_id=args.profile, material=args.material)
    (out_dir / "report.json").write_text(report.to_json())
    say("validate", report.passed, report.summary_line())

    if args.json:
        print(json.dumps({"out": str(out_dir), "notes": trace.notes,
                          "passed": report.passed}, indent=2))
        return 0 if report.passed else 1

    print()
    print(f"relief: {out_dir}")
    if not report.passed:
        print()
        print(report.agent_feedback())
    return 0 if report.passed else 1


# ---------------------------------------------------------------------------
# reconstruct
# ---------------------------------------------------------------------------


def _add_reconstruct(subparsers) -> None:
    parser = subparsers.add_parser(
        "reconstruct",
        help="image(s) to a 3D mesh via a locally hosted reconstruction model",
    )
    parser.add_argument("images", nargs="+")
    parser.add_argument(
        "--size",
        type=float,
        required=True,
        help="the real size of the object in mm; a reconstructed mesh has no units",
    )
    parser.add_argument("--axis", choices=["x", "y", "z"], default="z")
    parser.add_argument("--url", default="http://127.0.0.1:8300")
    parser.add_argument("--out", default="out")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--max-triangles", type=int, default=200_000)
    parser.add_argument("--format", default="glb", help="mesh format to ask the backend for")
    parser.add_argument(
        "--keep-floaters",
        action="store_true",
        help="keep disconnected pieces instead of treating them as noise",
    )
    parser.add_argument(
        "--no-lossy-repair",
        action="store_true",
        help="only geometry-preserving repair; a torn mesh then stays torn",
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE_ID)
    parser.add_argument("--material", default="PLA")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=_cmd_reconstruct)


def _cmd_reconstruct(args) -> int:
    from .reconstruct import (
        ReconstructError,
        ReconstructOptions,
        is_local,
        reconstruct,
    )
    from .validation import validate

    images = [Path(p) for p in args.images]
    missing = [str(p) for p in images if not p.exists()]
    if missing:
        print(_bad(f"no such image(s): {', '.join(missing)}"), file=sys.stderr)
        return 1

    opts = ReconstructOptions(
        url=args.url,
        size_mm=args.size,
        size_axis=args.axis,
        timeout_s=args.timeout,
        max_triangles=args.max_triangles,
        allow_lossy_repair=not args.no_lossy_repair,
        keep_floaters=args.keep_floaters,
        output_format=args.format,
    )

    def say(phase: str, ok: bool, message: str) -> None:
        if not args.json:
            print(f"  [{_ok('ok') if ok else _bad('!!')}] {phase:<9} {message}")

    if not is_local(args.url):
        say("network", True, f"sending {len(images)} image(s) off this machine to {args.url}")

    say("request", True, f"{len(images)} view(s) to {args.url}")
    try:
        result = reconstruct(images, opts)
    except ReconstructError as exc:
        print(_bad(str(exc)), file=sys.stderr)
        return 1

    say("mesh", True, f"backend returned {result.raw_triangles} triangles")
    for note in result.notes:
        say("clean", True, note)

    mesh = result.mesh
    out_dir = Path(args.out) / images[0].stem
    out_dir.mkdir(parents=True, exist_ok=True)
    stl = out_dir / "model.stl"
    mesh.export(str(stl))
    # 3MF is the better container, but trimesh needs lxml to write one and
    # that is not worth a hard dependency on a path whose essential output is
    # the mesh itself.
    try:
        mesh.export(str(out_dir / "model.3mf"))
    except Exception as exc:
        say("export", True, f"STL only; no 3MF ({type(exc).__name__})")
    (out_dir / "reconstruction.json").write_text(
        json.dumps(
            {
                "backend": result.backend,
                "views": [str(p) for p in images],
                "size_mm": args.size,
                "size_axis": args.axis,
                "raw_triangles": result.raw_triangles,
                "dropped_components": result.dropped_components,
                "repair": result.repair,
                "notes": result.notes,
            },
            indent=2,
        )
    )

    report = validate(str(stl), profile_id=args.profile, material=args.material)
    (out_dir / "report.json").write_text(report.to_json())
    say("validate", report.passed, report.summary_line())

    if args.json:
        print(json.dumps({"out": str(out_dir), "passed": report.passed,
                          "notes": result.notes}, indent=2))
        return 0 if report.passed else 1

    print()
    print(f"mesh: {out_dir}")
    # Said once, here, because it is the difference between this path and every
    # other one in the system.
    print(
        "no STEP and no source.py: this came from a mesh, so there is no B-rep "
        "to export and no script to re-run."
    )
    if not report.passed:
        print()
        print(report.agent_feedback())
    return 0 if report.passed else 1


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


def _add_models(subparsers) -> None:
    parser = subparsers.add_parser(
        "models", help="what the configured model endpoint actually offers"
    )
    parser.add_argument("--url", help="override FORMFORGE_LLM_BASE_URL")
    parser.add_argument("--free", action="store_true", help="only models priced at zero")
    parser.add_argument("--vision", action="store_true", help="only models that accept images")
    parser.add_argument("--grep", help="substring filter on the model id")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=_cmd_models)


def _cmd_models(args) -> int:
    from .llm import LLMError, OpenAICompatibleClient

    client = OpenAICompatibleClient(base_url=args.url) if args.url else OpenAICompatibleClient()
    try:
        found = client.list_models()
    except LLMError as exc:
        print(_bad(str(exc)), file=sys.stderr)
        return 1

    if args.free:
        found = [m for m in found if m["free"]]
    if args.vision:
        found = [m for m in found if m["vision"]]
    if args.grep:
        needle = args.grep.lower()
        found = [m for m in found if needle in m["id"].lower()]

    if args.json:
        print(json.dumps(found, indent=2))
        return 0 if found else 1

    if not found:
        print(_warn(f"no models matched at {client.base_url}"))
        return 1

    print(f"{len(found)} model(s) at {client.base_url}")
    print()
    for model in found:
        tags = []
        if model["free"]:
            tags.append(_ok("free"))
        if model["vision"]:
            tags.append("vision")
        if model["context"]:
            tags.append(f"{model['context']:,} ctx")
        suffix = f"  [{', '.join(tags)}]" if tags else ""
        print(f"  {model['id']}{suffix}")
    print()
    print("Set one with FORMFORGE_LLM_MODEL, or per tier with")
    print("FORMFORGE_LLM_MODEL_FAST / _STANDARD / _ESCALATED.")
    return 0


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
    from .store import Store

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
    from .store import PRINT_ISSUES, Store

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
    from .sandbox import GeometrySandbox

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
    if not described.get("rlimits", True):
        # Windows has no `resource` module, so nothing caps CPU or memory on
        # the subprocess path there. Worth its own line: the wall-clock
        # timeout still fires, but a runaway allocation is bounded by the host
        # and nothing else.
        print(
            f"  rlimits        {_warn('unavailable')} -- no CPU or memory ceiling "
            "on this platform; only the wall-clock timeout bounds a run"
        )

    client = build_client()
    if client.available:
        # Name the backend rather than assuming Claude: a local model behind an
        # OpenAI-compatible endpoint is now a first-class option, and reporting
        # it as "claude api configured" would be wrong in the one line someone
        # reads to find out what is actually wired up.
        from .llm import OpenAICompatibleClient

        if isinstance(client, OpenAICompatibleClient):
            model = client.models.get(Tier.STANDARD, "?")
            print(f"  model          {_ok('local')} -- {model} at {client.base_url}")
        else:
            print(f"  model          {_ok('claude api')}")
    else:
        print(
            f"  model          {_warn('not configured')} -- template path only; "
            "no intent parsing, freeform generation or visual critique"
        )

    print(f"  slicer         {_ok('found') if slicer_available() else _warn('not installed')}")

    try:
        import rtree  # noqa: F401

        print(f"  wall thickness {_ok('accelerated')}")
    except ImportError:
        print(f"  wall thickness {_warn('unaccelerated')} -- install rtree for full resolution")

    print(f"  profiles       {', '.join(sorted(PROFILES))}")

    from .store import DEFAULT_PATH, Store

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
