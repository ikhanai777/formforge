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
    _add_bootstrap(subparsers)
    _add_serve(subparsers)
    _add_account(subparsers)
    _add_artifacts(subparsers)
    _add_outbox(subparsers)
    _add_preflight(subparsers)
    _add_backup(subparsers)

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


# ---------------------------------------------------------------------------
# Operator commands
# ---------------------------------------------------------------------------
# A CLI rather than an HTTP admin panel, deliberately. An authenticated admin
# endpoint is a second privilege tier, a second auth path and a permanent
# target on the public surface; a command against the same database needs none
# of that and is enough for one operator. Revisit when there is a support team.
#
# Everything here reaches the account store directly. There is no way to run it
# without already having the database, which is the access control.
def _accounts():
    from .accounts import AccountStore

    return AccountStore()


def _add_bootstrap(subparsers) -> None:
    parser = subparsers.add_parser(
        "bootstrap",
        help="prepare a local install: directories, database, migrations",
        description=(
            "Everything a clean clone needs before it can serve a request. "
            "Safe to re-run: migrations are recorded and directories are made "
            "only if absent."
        ),
    )
    parser.add_argument(
        "--demo-user", metavar="EMAIL",
        help="create an account and print a generated password",
    )
    parser.set_defaults(handler=_cmd_bootstrap)


def _cmd_bootstrap(args) -> int:
    import secrets

    from .accounts import DuplicateEmail
    from .accounts.email import open_mailer
    from .config import Settings

    settings = Settings.from_env()
    print(f"mode: {settings.mode.value}")

    for label, path in (
        ("models", settings.model_dir),
        ("artifacts", None if settings.artifacts.startswith("s3://")
                      else Path(settings.artifacts)),
        ("outbox", settings.email_outbox),
    ):
        if path is None:
            print(f"  {label:10s} s3, nothing to create")
            continue
        path.mkdir(parents=True, exist_ok=True)
        print(f"  {label:10s} {path}")

    store = _accounts()
    print(f"  database   {settings.accounts_db} ({store.backend})")
    print(f"  migrations applied: {', '.join(_applied(store)) or 'none'}")

    if args.demo_user:
        password = secrets.token_urlsafe(18)
        try:
            user = store.create_user(args.demo_user, password)
        except DuplicateEmail:
            print(f"\n  {args.demo_user} already exists; leaving it alone")
        else:
            # Printed once, to a terminal, never logged. The alternative is a
            # fixed default password in a repo, which is how a demo account
            # becomes a production account.
            print(f"\n  demo account: {user['email']}")
            print(f"  password:     {password}")
            print("  (shown once, not stored anywhere else)")

    mailer = open_mailer(settings)
    print(f"\nemail: {mailer.name}", end="")
    print(f" -> {settings.email_outbox}" if mailer.name == "outbox" else "")
    problems = store and settings.problems()
    if problems and settings.mode.is_deployed:
        print("\nconfiguration problems:")
        for problem in problems:
            print(f"  - {problem}")
    print("\nNext: formforge serve      (or: python -m pytest tests/ -q)")
    return 0


def _applied(store) -> list[str]:
    with store._db.reader() as conn:
        rows = conn.execute("SELECT id FROM schema_migrations ORDER BY id").fetchall()
    return [dict(r)["id"] for r in rows]


def _add_serve(subparsers) -> None:
    parser = subparsers.add_parser(
        "serve", help="run the HTTP gateway",
        description="Starts uvicorn. Accounts are off unless --accounts is given.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--accounts", action="store_true",
                        help="enable signup, sessions, credits and billing")
    parser.add_argument("--reload", action="store_true")
    parser.set_defaults(handler=_cmd_serve)


def _cmd_serve(args) -> int:
    try:
        import uvicorn
    except ImportError:
        print(_warn("uvicorn is not installed. pip install 'formforge[api]'"))
        return 1
    from .api.app import create_app
    from .config import Settings
    from .logs import configure

    configure()
    settings = Settings.from_env()
    # Binding to localhost by default: a development server that defaults to
    # 0.0.0.0 is one `--host` away from being on a hotel network.
    app = create_app(accounts=_accounts() if args.accounts else None, settings=settings)
    print(f"http://{args.host}:{args.port}  mode={settings.mode.value} "
          f"accounts={'on' if args.accounts else 'off'}")
    uvicorn.run(app, host=args.host, port=args.port, reload=args.reload)
    return 0


def _add_account(subparsers) -> None:
    parser = subparsers.add_parser(
        "account", help="inspect and manage one account",
        description="The operator's window onto an account, its credits and its history.",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    show = sub.add_parser("show", help="plan, balance, ledger and audit trail")
    show.add_argument("email")
    show.set_defaults(handler=_cmd_account_show)

    create = sub.add_parser("create", help="create an account")
    create.add_argument("email")
    create.add_argument("--plan", default="free", choices=("free", "maker", "studio"))
    create.set_defaults(handler=_cmd_account_create)

    reset = sub.add_parser(
        "reset-password", help="set a new password and sign it out everywhere"
    )
    reset.add_argument("email")
    reset.set_defaults(handler=_cmd_account_reset)

    close = sub.add_parser("close", help="soft close: refuse logins, keep the ledger")
    close.add_argument("email")
    close.set_defaults(handler=_cmd_account_close)

    reopen = sub.add_parser("reopen", help="undo a close")
    reopen.add_argument("email")
    reopen.set_defaults(handler=_cmd_account_reopen)

    grant = sub.add_parser("grant", help="add credits by hand (recorded in the ledger)")
    grant.add_argument("email")
    grant.add_argument("credits", type=int)
    grant.add_argument("--note", default="operator grant")
    grant.set_defaults(handler=_cmd_account_grant)


def _require_account(store, email: str):
    user = store.get_user_by_email(email)
    if user is None:
        print(_warn(f"no account for {email}"))
        raise SystemExit(1)
    return user


def _cmd_account_show(args) -> int:
    store = _accounts()
    user = _require_account(store, args.email)
    account = store.account(user["id"])
    print(f"{account['email']}")
    print(f"  plan       {account['plan']} ({account['plan_status']})")
    print(f"  credits    {account['balance']}")
    print(f"  created    {user['created_at']}")
    if user.get("closed_at"):
        print(_warn(f"  CLOSED     {user['closed_at']}"))
    print(f"  billing id {user.get('billing_customer_id') or '-'}")

    print("\n  ledger")
    for entry in store.ledger(user["id"], limit=15):
        model = f"  {entry.model_id}" if entry.model_id else ""
        print(f"    {entry.created_at}  {entry.reason:11s} {entry.delta:+4d}{model}")

    trail = store.audit_trail(user["id"], limit=10)
    if trail:
        print("\n  audit")
        for row in trail:
            print(f"    {row['created_at']}  {row['action']:22s} {row['actor']}")
    return 0


def _cmd_account_create(args) -> int:
    import secrets

    from .accounts import DuplicateEmail

    store = _accounts()
    password = secrets.token_urlsafe(18)
    try:
        user = store.create_user(args.email, password, plan=args.plan)
    except DuplicateEmail:
        print(_warn(f"{args.email} already has an account"))
        return 1
    store.audit("account.created", user_id=user["id"], actor="operator",
                detail={"plan": args.plan})
    print(f"created {user['email']} on {args.plan}")
    print(f"password: {password}")
    print("(shown once)")
    return 0


def _cmd_account_reset(args) -> int:
    """Recover a locked-out account without email.

    The path an operator takes when the mail never arrives, and the reason a
    missing password-reset provider is survivable rather than fatal.
    """
    import secrets

    store = _accounts()
    user = _require_account(store, args.email)
    password = secrets.token_urlsafe(18)
    killed = store.reset_password(user["id"], password)
    store.audit("password.reset", user_id=user["id"], actor="operator",
                detail={"sessions_revoked": killed})
    print(f"new password for {user['email']}: {password}")
    print(f"{killed} session(s) revoked. Shown once; hand it over out of band.")
    return 0


def _cmd_account_close(args) -> int:
    store = _accounts()
    user = _require_account(store, args.email)
    killed = store.close_account(user["id"], actor="operator")
    print(f"closed {user['email']}; {killed} session(s) revoked")
    print("The credit ledger is kept -- it is a financial record. "
          "Use `account reopen` to undo.")
    return 0


def _cmd_account_reopen(args) -> int:
    store = _accounts()
    user = _require_account(store, args.email)
    store.reopen_account(user["id"], actor="operator")
    print(f"reopened {user['email']}")
    return 0


def _cmd_account_grant(args) -> int:
    store = _accounts()
    user = _require_account(store, args.email)
    entry = store.grant(user["id"], args.credits, note=args.note)
    store.audit("credits.granted", user_id=user["id"], actor="operator",
                detail={"credits": args.credits, "note": args.note})
    print(f"granted {entry.delta:+d}; balance is now {store.balance(user['id'])}")
    return 0


def _add_artifacts(subparsers) -> None:
    parser = subparsers.add_parser(
        "artifacts", help="inspect and clean up generated files",
        description=(
            "Retention is OFF unless FORMFORGE_RETENTION_DAYS is set above "
            "zero, and even then nothing is deleted without `sweep --confirm`."
        ),
    )
    sub = parser.add_subparsers(dest="action", required=True)

    show = sub.add_parser("show", help="what exists for one model")
    show.add_argument("model_id")
    show.set_defaults(handler=_cmd_artifacts_show)

    mark = sub.add_parser("mark", help="mark one model's files for deletion (reversible)")
    mark.add_argument("model_id")
    mark.set_defaults(handler=_cmd_artifacts_mark)

    sweep = sub.add_parser(
        "sweep", help="apply the retention policy",
        description="Lists candidates. Deletes nothing without --confirm.",
    )
    sweep.add_argument("--confirm", action="store_true",
                       help="actually delete the bytes of everything already marked")
    sweep.set_defaults(handler=_cmd_artifacts_sweep)


def _cmd_artifacts_show(args) -> int:
    store = _accounts()
    rows = store.artifacts_for(args.model_id)
    if not rows:
        print(f"no artifacts recorded for {args.model_id}")
        return 0
    for row in rows:
        print(f"  {row['fmt']:7s} {row['status']:15s} "
              f"{row['bytes']:>10,} B  {row['storage_key']}")
    return 0


def _cmd_artifacts_mark(args) -> int:
    store = _accounts()
    marked = store.mark_artifacts(args.model_id, actor="operator")
    print(f"marked {marked} artifact(s) for {args.model_id}")
    print("Nothing is deleted yet. `artifacts sweep --confirm` removes the bytes.")
    return 0


def _cmd_artifacts_sweep(args) -> int:
    from .config import Settings
    from .storage import open_storage

    settings = Settings.from_env()
    store = _accounts()
    storage = open_storage(settings.artifacts)

    if settings.retention_days <= 0:
        print("retention is off (FORMFORGE_RETENTION_DAYS=0); nothing will be marked")
    else:
        stale = store.artifacts_older_than(settings.retention_days, only_unpaid=True)
        print(f"{len(stale)} unpaid artifact(s) older than "
              f"{settings.retention_days} days")
        for row in stale:
            store.mark_artifacts(row["model_id"], actor="operator:sweep")

    pending = store.artifacts_pending_delete()
    print(f"{len(pending)} artifact(s) marked for deletion")
    if not args.confirm:
        for row in pending[:20]:
            print(f"  would delete  {row['model_id']} {row['fmt']}  {row['storage_key']}")
        print("\nNothing deleted. Re-run with --confirm to remove the bytes.")
        return 0

    removed = 0
    for row in pending:
        try:
            storage.delete(row["storage_key"])
        except Exception as exc:
            print(_warn(f"  could not delete {row['storage_key']}: {exc}"))
            continue
        # Idempotent: deleting something already gone is a success, and the
        # row is what stops it being reconsidered next sweep.
        store.finish_artifact_delete(row["id"])
        removed += 1
    print(f"deleted {removed} artifact(s). The records remain, marked deleted.")
    return 0


def _add_preflight(subparsers) -> None:
    parser = subparsers.add_parser(
        "preflight",
        help="check whether an environment is ready to serve",
        description=(
            "Validates configuration shape and, where the settings exist, "
            "whether the thing they name actually answers. Probes nothing "
            "that is not configured, prints no secret, and refuses to verify "
            "live billing."
        ),
    )
    parser.add_argument("--environment", default="staging",
                        choices=("local", "staging", "production"))
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.set_defaults(handler=_cmd_preflight)


def _cmd_preflight(args) -> int:
    import json as _json

    from .preflight import render, run

    report = run(args.environment)
    if args.json:
        print(_json.dumps(report.as_dict(), indent=2))
    else:
        print(render(report))
    # Non-zero when something *configured* is broken. A missing setting is a
    # deployment that is not finished, which is a different thing from one that
    # is wrong, and a CI gate should be able to tell them apart.
    return 1 if report.failed else 0


def _add_backup(subparsers) -> None:
    parser = subparsers.add_parser(
        "backup", help="back up and restore the accounts database",
        description=(
            "The ledger, the billing events and the identities -- the rows "
            "that cannot be rebuilt from source.py and params.json. Backends "
            "are detected from FORMFORGE_ACCOUNTS_DB: SQLite gets an online "
            "snapshot in a tar.gz, PostgreSQL gets pg_dump -Fc. Restore never "
            "runs without --confirm."
        ),
    )
    sub = parser.add_subparsers(dest="action", required=True)

    create = sub.add_parser("create", help="write a backup archive")
    create.add_argument(
        "output", nargs="?",
        help="archive path (default: backups/<timestamp>.tar.gz or .dump)",
    )
    create.set_defaults(handler=_cmd_backup_create)

    verify = sub.add_parser(
        "verify", help="open an archive and check it is usable",
        description=(
            "Do this on a schedule. An unverified backup is a belief, and the "
            "moment you find out is the moment you needed it."
        ),
    )
    verify.add_argument("archive")
    verify.set_defaults(handler=_cmd_backup_verify)

    restore = sub.add_parser(
        "restore", help="restore from an archive (destructive)",
        description=(
            "Refuses without --confirm. For SQLite the current database is "
            "moved aside rather than deleted; for PostgreSQL the objects in "
            "the dump are dropped and recreated, so take a backup first."
        ),
    )
    restore.add_argument("archive")
    restore.add_argument("--confirm", action="store_true",
                         help="required: this overwrites the live database")
    restore.set_defaults(handler=_cmd_backup_restore)

    check = sub.add_parser(
        "check", help="post-restore validation: read through the real store",
        description=(
            "Opens the database the way the application does -- migrations, "
            "views and all -- so a schema the app cannot use fails here rather "
            "than on the first request."
        ),
    )
    check.set_defaults(handler=_cmd_backup_check)


def _backup_target() -> tuple[str, bool]:
    from .config import Settings

    settings = Settings.from_env()
    return settings.accounts_db, settings.accounts_is_postgres


def _cmd_backup_create(args) -> int:
    from datetime import datetime, timezone

    from .backup import BackupError, backup_local, backup_postgres

    target, is_postgres = _backup_target()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = ".dump" if is_postgres else ".tar.gz"
    output = Path(args.output) if args.output else Path("backups") / f"{stamp}{suffix}"

    try:
        if is_postgres:
            path = backup_postgres(target, output)
            print(f"wrote {path} ({path.stat().st_size:,} B) from PostgreSQL")
            print("verify it with: formforge backup verify " + str(path))
        else:
            info = backup_local(target, output)
            print(f"wrote {info.path} ({info.path.stat().st_size:,} B)")
            print(f"  {info.summary}")
    except BackupError as exc:
        print(_warn(str(exc)))
        return 1
    return 0


def _cmd_backup_verify(args) -> int:
    from .backup import (
        BackupError,
        migration_compatibility,
        verify_backup,
        verify_postgres_dump,
    )

    path = Path(args.archive)
    try:
        if path.suffix == ".dump":
            tables = verify_postgres_dump(path)
            print(f"{path.name} is a readable pg_dump archive")
            print(f"  {len(tables)} table(s): {', '.join(tables)}")
            missing = [t for t in ("users", "credit_ledger") if t not in tables]
            if missing:
                print(_warn(f"  missing critical table(s): {', '.join(missing)}"))
                return 1
            return 0

        info = verify_backup(path)
        print(f"{path.name} is a usable FormForge backup")
        print(f"  taken       {info.created_at}")
        print(f"  by          formforge {info.formforge_version}")
        print(f"  revisions   {', '.join(info.revisions) or 'none'}")
        for table, count in info.rows.items():
            print(f"  {table:20s} {count:>8,} row(s)")
        compatible, reason = migration_compatibility(info)
        print(("  " if compatible else _warn("  ")) + reason)
        return 0 if compatible else 1
    except BackupError as exc:
        print(_warn(str(exc)))
        return 1


def _cmd_backup_restore(args) -> int:
    from .backup import BackupError, restore_local, restore_postgres

    target, is_postgres = _backup_target()
    if not args.confirm:
        where = "the PostgreSQL database" if is_postgres else target
        print(_warn(f"this would overwrite {where}."))
        print("Nothing done. Re-run with --confirm once you are sure, and take "
              "a `formforge backup create` of the current state first.")
        return 1

    try:
        if is_postgres:
            restore_postgres(target, args.archive, confirm=True)
            print("pg_restore finished")
        else:
            info = restore_local(args.archive, target, confirm=True)
            print(f"restored {info.summary}")
    except BackupError as exc:
        print(_warn(str(exc)))
        return 1
    print("now run: formforge backup check")
    return 0


def _cmd_backup_check(args) -> int:
    from .backup import BackupError, post_restore_check

    target, _ = _backup_target()
    try:
        result = post_restore_check(target)
    except BackupError as exc:
        print(_warn(str(exc)))
        return 1
    for key, value in result.items():
        printable = ", ".join(value) if isinstance(value, list) else f"{value:,}"
        print(f"  {key:24s} {printable}")
    if result["users"] == 0:
        print(_warn("no users. If that is not what you restored, stop here."))
        return 1
    return 0


def _add_outbox(subparsers) -> None:
    parser = subparsers.add_parser(
        "outbox", help="read the local email outbox",
        description=(
            "What a local run actually sent. Only meaningful when "
            "FORMFORGE_EMAIL=outbox."
        ),
    )
    parser.add_argument("-n", "--count", type=int, default=3)
    parser.set_defaults(handler=_cmd_outbox)


def _cmd_outbox(args) -> int:
    from .accounts.email import OutboxMailer
    from .config import Settings

    settings = Settings.from_env()
    box = OutboxMailer(settings.email_outbox)
    messages = box.read_all()
    if not messages:
        print(f"outbox is empty ({settings.email_outbox})")
        return 0
    for message in messages[-args.count:]:
        print(message)
        print("-" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
