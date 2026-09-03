"""Command line interface.

    formforge generate "a hex planter for a 4 inch pot"
    formforge build keychain_text_tag --set text=RIVER --set body_l_mm=70
    formforge mushroom --count 6 --seed 42 --species mixed --out out/mushrooms
    formforge vase --count 8 --style mixed --formats stl,step
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
    _add_generators(subparsers)
    _add_templates(subparsers)
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
# generators (one definition, a population of models)
# ---------------------------------------------------------------------------


def _add_generators(subparsers) -> None:
    """One subcommand per generator, from the catalog.

    The two definitions differ in their domain and in nothing else the command
    line cares about, so the command is written once and the catalog supplies
    the noun -- `--species` for mushrooms, `--style` for vases.
    """
    from .generators import CATALOG  # noqa: PLC0415

    for generator in CATALOG:
        _add_generator(subparsers, generator)


def _add_generator(subparsers, generator) -> None:
    parser = subparsers.add_parser(generator.name, help=generator.summary)
    parser.add_argument("--count", type=int, default=6, help="how many to generate")
    parser.add_argument("--seed", type=int, default=7, help="the population's seed")
    parser.add_argument(
        f"--{generator.variant_flag}",
        dest="variant",
        default="mixed",
        help=f"one of the definition's {generator.variant_noun} values, or 'mixed' "
        f"to draw one per model",
    )
    parser.add_argument(
        "--variation",
        type=float,
        default=0.55,
        help=f"0 rebuilds the {generator.variant_noun} exactly; 1 lets every slider "
        f"wander its full range",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="pin a parameter across the whole population; repeatable",
    )
    parser.add_argument(
        "--out", default=f"out/{generator.name}s", help="directory for the exported files"
    )
    parser.add_argument(
        "--formats",
        default="stl,step,3mf",
        help="which exports to keep per specimen: stl (print), step (edit in CAD), "
        "3mf (print, declares its units). Default keeps all three.",
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE_ID)
    parser.add_argument("--material", default="PLA")
    parser.add_argument(
        "--params-only",
        action="store_true",
        help="print the parameter sets without building anything",
    )
    parser.add_argument("--render", action="store_true", help="also write a preview PNG each")
    parser.add_argument(
        "--explain", action="store_true", help="print the definition graph and exit"
    )
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=_cmd_generator, generator=generator.name)


def _cmd_generator(args) -> int:
    from .generators import catalog  # noqa: PLC0415

    generator = catalog()[args.generator]

    if args.explain:
        print(generator.definition.explain())
        return 0

    registry = TemplateRegistry.load(strict=False)
    try:
        template = registry.get(generator.template_id)
    except KeyError as exc:
        print(_bad(str(exc)), file=sys.stderr)
        return 1

    try:
        pins = _parse_settings(args.set, template)
    except SystemExit as exc:
        print(_bad(str(exc)), file=sys.stderr)
        return 1

    try:
        solutions = [
            generator.solve(
                generator.member_seed(args.seed, index),
                variant=args.variant,
                variation=args.variation,
                overrides=pins,
            )
            for index in range(max(0, args.count))
        ]
    except ValueError as exc:
        print(_bad(str(exc)), file=sys.stderr)
        return 1

    specimens = [
        {
            "index": index,
            "variant": generator.variant_of(solution),
            "seed": solution["params"]["seed"],
            "params": solution["params"],
        }
        for index, solution in enumerate(solutions)
    ]

    if args.params_only:
        if args.json:
            print(json.dumps(specimens, indent=2))
            return 0
        for specimen in specimens:
            print(f"  {specimen['index']:>2}  {specimen['variant']:<12} "
                  f"seed {specimen['seed']:<5} {generator.describe(specimen['params'])}")
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    formats = [f.strip().lower() for f in args.formats.split(",") if f.strip()]
    unknown = [f for f in formats if f not in {"stl", "step", "3mf"}]
    if unknown:
        print(_bad(f"unknown format(s): {', '.join(unknown)}. Known: stl, step, 3mf."),
              file=sys.stderr)
        return 1
    if "stl" not in formats:
        # The DFM verdict is measured on the mesh, so the STL is written either
        # way; --formats decides what is kept beside it.
        formats.insert(0, "stl")
    built = _build_specimens(specimens, template, out_dir, args, formats, generator)

    manifest = out_dir / "variations.json"
    manifest.write_text(
        json.dumps(
            {
                "template": template.id,
                "definition": generator.definition.name,
                "seed": args.seed,
                generator.variant_flag: args.variant,
                "variation": args.variation,
                "pinned": pins,
                "formats": formats,
                "specimens": built,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    if args.json:
        print(json.dumps(built, indent=2))
    else:
        ok = sum(1 for b in built if b["status"] == "ok")
        print()
        print(f"{ok}/{len(built)} built into {out_dir} as {', '.join(formats)}")
        print(_dim(f"parameters and verdicts: {manifest}"))
    return 0 if all(b["status"] == "ok" for b in built) else 1


def _build_specimens(
    specimens: list[dict], template, out_dir: Path, args, wanted_formats: list[str], generator
) -> list[dict]:
    """Build each model in the sandbox and validate what came out."""
    import shutil  # noqa: PLC0415

    from .sandbox import ExecuteRequest, GeometrySandbox  # noqa: PLC0415

    sandbox = GeometrySandbox()
    built: list[dict] = []
    for specimen in specimens:
        params = specimen["params"]
        name = f"{specimen['index']:02d}-{specimen['variant']}-{specimen['seed']}"
        record = {**{k: v for k, v in specimen.items() if k != "params"}, "params": params}

        problems = template.validate_params(params)
        if problems:
            record.update(status="rejected", detail="; ".join(problems))
            built.append(record)
            if not args.json:
                print(f"  [{_bad('!!')}] {name}: {problems[0]}")
            continue

        execution = sandbox.execute(
            ExecuteRequest(
                source=template.render_source(params),
                language=template.language,
                params=params,
                # Hand-authored templates are human-reviewed; the magic-number
                # style rule does not apply to them.
                enforce_named_constants=False,
            )
        )
        if not execution.ok:
            record.update(
                status="failed", detail=f"{execution.error_class}: {execution.message}"
            )
            built.append(record)
            if not args.json:
                print(f"  [{_bad('!!')}] {name}: {execution.message}")
            continue

        # The kernel exports STL, STEP and 3MF on every build; which of them
        # survive is the caller's choice. STEP is the one that opens in CAD
        # with its faces and edges intact, so it is kept by default.
        stl = out_dir / f"{name}.stl"
        shutil.copyfile(execution.artifacts["stl"], stl)
        for fmt in wanted_formats:
            source = execution.artifacts.get(fmt)
            if fmt == "stl" or not source:
                continue
            copy = out_dir / f"{name}.{fmt}"
            shutil.copyfile(source, copy)
            record[fmt] = str(copy)
        missing = [f for f in wanted_formats if f != "stl" and not execution.artifacts.get(f)]
        for fmt in missing:
            record[f"{fmt}_error"] = execution.artifacts.get(f"{fmt}_error", "not exported")

        report = validate(
            str(stl),
            profile_id=args.profile,
            material=args.material,
            category=template.category,
            params=params,
            template_invariants=template.invariants,
            expected_solids=template.expected_solids,
            brep_features=execution.stats.get("brep_features"),
        )
        record.update(
            status="ok" if report.passed else "unprintable",
            stl=str(stl),
            bbox_mm=execution.stats.get("bbox_mm"),
            triangles=execution.stats.get("triangles"),
            failures=[c.id for c in report.hard_failures],
            warnings=[c.id for c in report.warnings],
        )

        if args.render:
            from .render import render_views  # noqa: PLC0415

            preview = render_views(str(stl), out_dir / name, views=("iso",))
            record["preview"] = preview.views.get("iso")

        built.append(record)
        if not args.json:
            marker = _ok("ok") if report.passed else _warn("??")
            print(f"  [{marker}] {name}: {generator.describe(params)}")
            for failure in record["failures"]:
                print(_warn(f"        {failure}"))
    return built


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
