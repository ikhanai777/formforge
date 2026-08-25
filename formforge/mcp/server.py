"""FormForge MCP server (spec section 9).

Mode B of the two integration modes: Claude uses FormForge. A user connects this
server and says "make me a hex wall planter for a 4-inch pot", and the chat
itself becomes the interface -- no web app, no signup flow, no UI to build.

Two design rules run through the tool layer, and both come from spec section
9.4:

* **Return images inline.** A `generate_*` or `render_views` result that carries
  the preview lets Claude see what it made and correct itself without a second
  round trip. It is also the difference between a chat interaction that feels
  like a design tool and one that feels like a file drop.
* **Keep text results structured and terse.** The DFM report goes back as a
  compact object, not prose. Claude narrates it to the user far better than a
  fixed template can, and prose in a tool result is tokens spent on something
  that gets rewritten anyway.

The tool descriptions matter as much as the schemas: `list_templates` says
outright that templates are faster, cheaper and verified, because steering
traffic toward the template path is both the reliability story and the primary
cost lever (spec section 16). Each template's `print_test_status` travels with
it, so a model presenting one to a user can say whether it has actually been
printed rather than implying it has.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from ..bundle import write_bundle
from ..dfm import DEFAULT_PROFILE_ID, PROFILES
from ..llm import build_client
from ..orchestrator import Orchestrator
from ..registry import TemplateRegistry
from ..render import STANDARD_VIEWS, render_views
from ..slicer import slice_model
from ..validation import validate

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_templates",
        "description": (
            "List available parametric product templates, optionally filtered by "
            "category or searched by free text. Call this first: a template that "
            "fits is faster, cheaper and verified, and it cannot produce broken "
            "geometry because the geometry is already written. Each result "
            "carries a print_test_status -- 'untested' means it validates but "
            "has never been physically printed, which is worth telling the user. "
            "Only fall back to generate_from_code when nothing here fits."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["keychain", "organizer", "planter", "wall_decor", "hook", "box"],
                },
                "query": {
                    "type": "string",
                    "description": "Free-text search over template names, descriptions and tags.",
                },
            },
        },
    },
    {
        "name": "get_template_schema",
        "description": (
            "Get the full parameter JSON Schema for one template: valid ranges, "
            "defaults, the DFM note attached to each parameter, and the "
            "cross-parameter requirements under 'requirements'. The notes explain "
            "why each default is what it is, so read them before choosing values."
        ),
        "input_schema": {
            "type": "object",
            "required": ["template_id"],
            "properties": {"template_id": {"type": "string"}},
        },
    },
    {
        "name": "generate_from_template",
        "description": (
            "Generate a model from a validated template. Returns a model_id, the "
            "achieved dimensions, the full DFM validation report, and preview "
            "images. This is the preferred generation path."
        ),
        "input_schema": {
            "type": "object",
            "required": ["template_id", "params"],
            "properties": {
                "template_id": {"type": "string"},
                "params": {"type": "object"},
                "printer_profile": {"type": "string", "default": DEFAULT_PROFILE_ID},
                "material": {
                    "type": "string",
                    "enum": ["PLA", "PETG", "ABS", "TPU", "ASA"],
                    "default": "PLA",
                },
            },
        },
    },
    {
        "name": "generate_from_code",
        "description": (
            "Execute a build123d script to produce a model. Use only when no "
            "template fits. Every dimension must be a named module-level "
            "constant -- a numeric literal inside a geometry call is rejected "
            "before the script runs, because that is what makes the result "
            "editable. The script runs with no network access and a 30 second "
            "CPU limit; only build123d, math and numpy are importable."
        ),
        "input_schema": {
            "type": "object",
            "required": ["language", "source"],
            "properties": {
                "language": {"type": "string", "enum": ["build123d", "openscad"]},
                "source": {"type": "string"},
                "exposed_params": {
                    "type": "object",
                    "description": "Map of constant name to {min, max, step, label} for UI sliders.",
                },
                "printer_profile": {"type": "string", "default": DEFAULT_PROFILE_ID},
                "material": {"type": "string", "default": "PLA"},
                "category": {
                    "type": "string",
                    "description": "Product category, so category-specific DFM rules apply.",
                },
            },
        },
    },
    {
        "name": "generate_from_prompt",
        "description": (
            "Generate a model from a natural-language description, running the "
            "full agent loop: intent parsing, template matching, validation and "
            "repair. Use this when the user describes what they want rather than "
            "naming a template."
        ),
        "input_schema": {
            "type": "object",
            "required": ["prompt"],
            "properties": {
                "prompt": {"type": "string"},
                "printer_profile": {"type": "string", "default": DEFAULT_PROFILE_ID},
                "material": {"type": "string", "default": "PLA"},
                "interactive": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "When true, returns a clarifying question rather than "
                        "guessing at a missing functional dimension."
                    ),
                },
            },
        },
    },
    {
        "name": "check_printability",
        "description": (
            "Run the full DFM validation suite on an existing model. Returns hard "
            "failures, warnings, and the specific numbers behind them: minimum "
            "wall thickness and where it is, overhang area, bridge spans, "
            "first-layer contact fraction."
        ),
        "input_schema": {
            "type": "object",
            "required": ["model_id"],
            "properties": {
                "model_id": {"type": "string"},
                "printer_profile": {"type": "string"},
                "material": {
                    "type": "string",
                    "enum": ["PLA", "PETG", "ABS", "TPU", "ASA"],
                    "default": "PLA",
                },
            },
        },
    },
    {
        "name": "render_views",
        "description": (
            "Render orthographic, isometric and section-cut views of a model. Use "
            "this to check the result matches what the user asked for before "
            "presenting it -- the section view is the only way to see inside a "
            "hollow part, and the isometric view carries a 10 mm build-plate grid "
            "so the scale is unambiguous."
        ),
        "input_schema": {
            "type": "object",
            "required": ["model_id"],
            "properties": {
                "model_id": {"type": "string"},
                "views": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(STANDARD_VIEWS)},
                    "default": ["iso", "front", "top", "section"],
                },
                "show_build_plate_grid": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "modify_model",
        "description": (
            "Change parameters on an existing model and regenerate. Cheaper and "
            "safer than generating from scratch, and it preserves the lineage so "
            "the model's history stays intact."
        ),
        "input_schema": {
            "type": "object",
            "required": ["model_id", "param_changes"],
            "properties": {
                "model_id": {"type": "string"},
                "param_changes": {"type": "object"},
            },
        },
    },
    {
        "name": "slice_preview",
        "description": (
            "Slice the model with a real slicer for accurate print time, filament "
            "use, layer count and support volume. The support-volume ratio is a "
            "design signal: above about 30%, the part is badly oriented rather "
            "than merely support-requiring."
        ),
        "input_schema": {
            "type": "object",
            "required": ["model_id"],
            "properties": {
                "model_id": {"type": "string"},
                "printer_profile": {"type": "string"},
                "quality": {
                    "type": "string",
                    "enum": ["draft", "standard", "fine"],
                    "default": "standard",
                },
                "supports": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "export_model",
        "description": (
            "Get the files for a model. 3MF is recommended over STL because it "
            "records its units -- an STL does not, which is the cause of every "
            "model that imports at 25.4 times the intended size."
        ),
        "input_schema": {
            "type": "object",
            "required": ["model_id"],
            "properties": {
                "model_id": {"type": "string"},
                "formats": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["3mf", "stl", "step", "source", "params", "report"],
                    },
                    "default": ["3mf", "stl"],
                },
            },
        },
    },
    {
        "name": "list_printer_profiles",
        "description": (
            "List the printer profiles the DFM rules can be evaluated against, "
            "with nozzle diameter, layer height and build volume."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


class ToolError(Exception):
    """A tool call failed in a way the caller should see as a message."""


class FormForgeTools:
    """The tool layer, shared by the MCP server and the HTTP API.

    Kept transport-agnostic on purpose: the same methods back the stdio MCP
    server, the hosted remote server and the REST API, so the three can never
    drift into having different behaviour for the same operation.
    """

    def __init__(
        self,
        *,
        registry: TemplateRegistry | None = None,
        orchestrator: Orchestrator | None = None,
        store_dir: Path | str | None = None,
    ):
        self.registry = registry or TemplateRegistry.load(strict=False)
        self.store = Path(store_dir or Path.home() / ".formforge" / "models")
        self.store.mkdir(parents=True, exist_ok=True)
        self.orchestrator = orchestrator or Orchestrator(
            registry=self.registry,
            client=build_client(),
            output_dir=self.store,
        )
        # model_id -> the GenerationResult that produced it, so follow-up tools
        # can act on a model without re-reading it off disk.
        self._models: dict[str, Any] = {}

    # -- discovery ---------------------------------------------------------
    def list_templates(self, category: str | None = None, query: str | None = None) -> dict:
        if query:
            matches = self.registry.search(query, category, limit=8)
            templates = [m.as_dict() for m in matches if m.score > 0.05]
        else:
            templates = [t.summary() for t in self.registry.list(category)]
        return {
            "count": len(templates),
            "templates": templates,
            "categories": self.registry.categories(),
        }

    def get_template_schema(self, template_id: str) -> dict:
        try:
            return self.registry.get(template_id).detail()
        except KeyError as exc:
            raise ToolError(str(exc)) from None

    def list_printer_profiles(self) -> dict:
        return {
            "default": DEFAULT_PROFILE_ID,
            "profiles": [
                {
                    "id": profile.id,
                    "display_name": profile.display_name,
                    "nozzle_mm": profile.nozzle_mm,
                    "layer_mm": profile.layer_mm,
                    "build_volume_mm": list(profile.build_volume_mm),
                }
                for profile in PROFILES.values()
            ],
        }

    # -- generation --------------------------------------------------------
    def generate_from_template(
        self,
        template_id: str,
        params: dict,
        printer_profile: str = DEFAULT_PROFILE_ID,
        material: str = "PLA",
    ) -> dict:
        try:
            template = self.registry.get(template_id)
        except KeyError as exc:
            raise ToolError(str(exc)) from None

        problems = template.validate_params(template.merge_params(params))
        if problems:
            raise ToolError(
                "these parameter values are outside the template's tested range: "
                + "; ".join(problems)
            )

        result = self.orchestrator.generate(
            prompt=f"{template.display_name} with the given parameters",
            printer_profile=printer_profile,
            material=material,
            interactive=False,
            template_id=template_id,
            params=params,
        )
        return self._record(result, template)

    def generate_from_prompt(
        self,
        prompt: str,
        printer_profile: str = DEFAULT_PROFILE_ID,
        material: str = "PLA",
        interactive: bool = True,
    ) -> dict:
        result = self.orchestrator.generate(
            prompt,
            printer_profile=printer_profile,
            material=material,
            interactive=interactive,
        )
        if result.status == "needs_clarification":
            return {
                "status": "needs_clarification",
                "questions": result.clarifications,
                "parsed_intent": result.intent,
            }
        if result.status == "refused":
            return {"status": "refused", "message": result.message}
        template = (
            self.registry.get(result.template_id)
            if result.template_id and result.template_id in self.registry
            else None
        )
        return self._record(result, template)

    def generate_from_code(
        self,
        language: str,
        source: str,
        exposed_params: dict | None = None,
        printer_profile: str = DEFAULT_PROFILE_ID,
        material: str = "PLA",
        category: str | None = None,
    ) -> dict:
        from ..sandbox import ExecuteRequest  # noqa: PLC0415

        execution = self.orchestrator.sandbox.execute(
            ExecuteRequest(
                source=source,
                language=language,
                metadata={"generator": "FormForge", "units": "millimeter"},
                enforce_named_constants=True,
            )
        )
        if not execution.ok:
            return {
                "status": "failed",
                "phase": execution.phase,
                "error_class": execution.error_class,
                "message": execution.message,
                "hint": execution.hint,
                "violations": execution.violations,
            }

        report = validate(
            execution.artifacts["stl"],
            profile_id=printer_profile,
            material=material,
            category=category,
            brep_features=execution.stats.get("brep_features"),
        )

        from ..orchestrator.loop import GenerationResult  # noqa: PLC0415
        import uuid  # noqa: PLC0415

        result = GenerationResult(
            model_id=uuid.uuid4().hex,
            status="ok" if report.passed else "failed",
            prompt="generated from supplied build123d source",
            route="freeform",
            source_code=source,
            language=language,
            params=exposed_params or {},
            artifacts=dict(execution.artifacts),
            stats=dict(execution.stats),
            validation=report.as_dict(),
            iterations=1,
            workdir=execution.workdir,
        )
        previews = render_views(
            execution.artifacts["stl"],
            self.store / result.model_id / "previews",
            views=("iso", "front", "section"),
        )
        result.previews = dict(previews.views)
        return self._record(result, None)

    def modify_model(self, model_id: str, param_changes: dict) -> dict:
        previous = self._get(model_id)
        template_id = previous.template_id
        if not template_id:
            raise ToolError(
                "this model was generated from freeform code rather than a "
                "template, so it has no parameter schema to modify. Call "
                "generate_from_code with an edited script instead."
            )
        merged = {**(previous.params or {}), **param_changes}
        outcome = self.generate_from_template(
            template_id,
            merged,
            printer_profile=previous.intent.get("printer_profile", DEFAULT_PROFILE_ID),
            material=previous.intent.get("material", "PLA"),
        )
        outcome["parent_model_id"] = model_id
        return outcome

    # -- inspection --------------------------------------------------------
    def check_printability(
        self,
        model_id: str,
        printer_profile: str | None = None,
        material: str = "PLA",
    ) -> dict:
        result = self._get(model_id)
        stl = result.artifacts.get("stl")
        if not stl or not Path(stl).exists():
            raise ToolError(f"model {model_id} has no mesh on disk to check")
        template = (
            self.registry.get(result.template_id)
            if result.template_id and result.template_id in self.registry
            else None
        )
        report = validate(
            stl,
            profile_id=printer_profile or result.intent.get("printer_profile"),
            material=material,
            category=result.intent.get("category"),
            params=result.params,
            template_invariants=template.invariants if template else None,
            expected_solids=template.expected_solids if template else 1,
            brep_features=result.stats.get("brep_features"),
        )
        return report.as_dict()

    def render_views(
        self,
        model_id: str,
        views: list[str] | None = None,
        show_build_plate_grid: bool = True,
    ) -> dict:
        result = self._get(model_id)
        stl = result.artifacts.get("stl")
        if not stl or not Path(stl).exists():
            raise ToolError(f"model {model_id} has no mesh on disk to render")
        rendered = render_views(
            stl,
            self.store / model_id / "previews",
            views=tuple(views or ("iso", "front", "top", "section")),
            show_build_plate_grid=show_build_plate_grid,
        )
        result.previews.update(rendered.views)
        return {"model_id": model_id, "views": rendered.views}

    def slice_preview(
        self,
        model_id: str,
        printer_profile: str | None = None,
        quality: str = "standard",
        supports: bool = False,
    ) -> dict:
        result = self._get(model_id)
        source = result.artifacts.get("3mf") or result.artifacts.get("stl")
        if not source:
            raise ToolError(f"model {model_id} has no mesh to slice")
        summary = slice_model(
            source,
            profile_id=printer_profile or result.intent.get("printer_profile", DEFAULT_PROFILE_ID),
            quality=quality,
            supports=supports,
            out_dir=self.store / model_id,
        )
        return summary.as_dict()

    def export_model(self, model_id: str, formats: list[str] | None = None) -> dict:
        result = self._get(model_id)
        bundle_dir = self.store / model_id / "bundle"
        template = (
            self.registry.get(result.template_id)
            if result.template_id and result.template_id in self.registry
            else None
        )
        bundle = write_bundle(result, bundle_dir, template=template)
        wanted = set(formats or ["3mf", "stl"])
        return {
            "model_id": model_id,
            "directory": str(bundle.directory),
            "files": {k: v for k, v in bundle.files.items() if k in wanted or k == "readme"},
            "note": (
                "3MF records its units and print settings; STL does not. If a "
                "slicer imports the STL at the wrong scale, the model is in "
                "millimetres."
            ),
        }

    # -- internals ---------------------------------------------------------
    def _record(self, result, template) -> dict:
        self._models[result.model_id] = result
        payload = {
            "model_id": result.model_id,
            "status": result.status,
            "template_id": result.template_id,
            "route": result.route,
            "iterations": result.iterations,
            "dimensions_mm": result.stats.get("bbox_mm"),
            "volume_mm3": result.stats.get("volume_mm3"),
            "triangles": result.stats.get("triangles"),
            "params": result.params,
            "validation": _compact_report(result.validation),
            "previews": result.previews,
            "message": result.message,
        }
        if result.critique and result.critique.get("ran"):
            payload["visual_check"] = {
                "verdict": result.critique["verdict"],
                "summary": result.critique["summary"],
            }
        return payload

    def _get(self, model_id: str):
        try:
            return self._models[model_id]
        except KeyError:
            raise ToolError(
                f"no model {model_id} in this session. Model ids come from a "
                "generate_* call and do not persist across restarts."
            ) from None

    # -- dispatch ----------------------------------------------------------
    def call(self, name: str, arguments: dict) -> dict:
        handler = {
            "list_templates": self.list_templates,
            "get_template_schema": self.get_template_schema,
            "list_printer_profiles": self.list_printer_profiles,
            "generate_from_template": self.generate_from_template,
            "generate_from_prompt": self.generate_from_prompt,
            "generate_from_code": self.generate_from_code,
            "modify_model": self.modify_model,
            "check_printability": self.check_printability,
            "render_views": self.render_views,
            "slice_preview": self.slice_preview,
            "export_model": self.export_model,
        }.get(name)
        if handler is None:
            raise ToolError(f"unknown tool {name!r}")
        return handler(**arguments)


def _compact_report(report: dict | None) -> dict:
    """Shrink the validation report for a tool result.

    The full report carries every passing check, which is most of it and none of
    what a reader needs. Failures and warnings carry the information; the rest is
    a count.
    """
    if not report:
        return {}
    return {
        "passed": report.get("passed"),
        "summary": report.get("summary"),
        "failures": [
            {"id": c["id"], "message": c["message"], "remedy": c.get("remedy")}
            for c in report.get("hard_failures", [])
        ],
        "warnings": [
            {"id": c["id"], "message": c["message"]} for c in report.get("warnings", [])
        ],
        "key_measurements": {
            key: report.get("measurements", {}).get(key)
            for key in (
                "bbox_mm",
                "min_wall_mm",
                "max_overhang_deg",
                "max_bridge_mm",
                "plate_contact_fraction",
                "triangles",
            )
            if report.get("measurements", {}).get(key) is not None
        },
    }


def image_content_block(path: str | Path) -> dict:
    """An MCP image content block, for returning previews inline.

    Spec section 9.4: a tool result carrying the preview lets Claude self-correct
    without a second round trip, and makes the chat-native flow feel like a
    design tool rather than a file drop.
    """
    data = base64.standard_b64encode(Path(path).read_bytes()).decode("ascii")
    return {"type": "image", "data": data, "mimeType": "image/png"}


def result_content(payload: dict, *, include_images: bool = True) -> list[dict]:
    """Turn a tool result into MCP content blocks."""
    blocks: list[dict] = [
        {"type": "text", "text": json.dumps(payload, indent=2, default=str)}
    ]
    if not include_images:
        return blocks
    previews = payload.get("previews") or payload.get("views") or {}
    for name in ("iso", "section", "front", "top"):
        path = previews.get(name)
        if path and Path(path).exists():
            blocks.append({"type": "text", "text": f"{name} view:"})
            blocks.append(image_content_block(path))
    return blocks
