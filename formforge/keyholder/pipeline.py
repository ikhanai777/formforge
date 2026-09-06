"""Image in, bundle out.

The same five stages the rest of FormForge runs -- generate, execute, validate,
render, report -- with one difference: the repair loop needs no model. Every
failure a traced silhouette produces has a known cause and a known adjustment,
so the loop is a lookup table rather than a conversation:

    a wall under the minimum       -> open the outline further
    more than one solid            -> close the gaps, deepen the rail overlap
    the kernel refused the profile -> simplify harder

That makes the whole feature run offline, at zero token cost, deterministically:
the same image and the same parameters produce the same STL, byte for byte. It
also keeps the loop honest. When the ladder runs out, the result carries the
validator's own words rather than a fourth guess.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..dfm import DEFAULT_PROFILE_ID
from ..orchestrator.loop import GenerationResult, LoopEvent
from ..render import STANDARD_VIEWS, render_views
from ..sandbox import ExecuteRequest, GeometrySandbox, Limits, Tessellation
from ..validation import validate
from .design import (
    DETAIL_MODES,
    MOUNT_MODES,
    PARAM_RANGES,
    DesignError,
    KeyHolderPlan,
    KeyHolderSpec,
)
from .design import (
    plan as plan_layout,
)
from .emit import emit
from .image import DEFAULT_MAX_DIM, MaskInfo, read_mask, smooth
from .trace import TraceStats, trace_polygon

# The category the DFM rules and the tier-3 invariants are evaluated against.
CATEGORY = "key_holder"

# Attempts, including the first. Three is enough for the ladder below to run
# out; a fourth would be a fourth guess at the same problem.
MAX_ATTEMPTS = 3

ProgressCallback = Callable[[LoopEvent], None]


@dataclass
class KeyHolderResult:
    """The generation, plus what the image contributed to it."""

    result: GenerationResult
    plan: KeyHolderPlan | None = None
    mask_info: MaskInfo | None = None
    trace_stats: TraceStats | None = None
    attempts: int = 0
    adjustments: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.result.ok

    @property
    def model_id(self) -> str:
        return self.result.model_id

    def as_dict(self) -> dict[str, Any]:
        payload = self.result.as_dict()
        payload["keyholder"] = {
            "plan": self.plan.as_dict() if self.plan else None,
            "image": self.mask_info.as_dict() if self.mask_info else None,
            "trace": self.trace_stats.as_dict() if self.trace_stats else None,
            "attempts": self.attempts,
            "adjustments": list(self.adjustments),
        }
        return payload

    def summary(self) -> str:
        if not self.ok or not self.plan:
            # A layout that never became a solid is not a key holder, and
            # describing it as one is how a failed run gets reported as a
            # success by a caller that only prints the summary.
            return self.result.summary()
        lines = [f"Built a key holder -- {self.plan.summary()}."]
        if self.adjustments:
            lines.append("Adjusted along the way: " + "; ".join(self.adjustments) + ".")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------


def trace_image(
    source: str | Path | bytes,
    *,
    threshold: float | None = None,
    invert: bool | None = None,
    max_dim: int = DEFAULT_MAX_DIM,
    simplify_px: float = 0.6,
):
    """Image -> (silhouette polygon in pixels, mask info, trace stats)."""
    mask = read_mask(source, threshold=threshold, invert=invert, max_dim=max_dim)
    polygon, stats = trace_polygon(smooth(mask.array), simplify_px=simplify_px)
    return polygon, mask, stats


def plan_from_image(
    source: str | Path | bytes,
    spec: KeyHolderSpec | None = None,
    *,
    threshold: float | None = None,
    invert: bool | None = None,
    max_dim: int = DEFAULT_MAX_DIM,
) -> tuple[KeyHolderPlan, MaskInfo, TraceStats]:
    """Trace an image and lay out a key holder, without building anything.

    Useful on its own: it is fast, it needs no CAD kernel, and it answers the
    question the user actually has after uploading a photograph -- "did it see
    the right shape?".
    """
    spec = spec or KeyHolderSpec()
    polygon, mask, stats = trace_image(
        source, threshold=threshold, invert=invert, max_dim=max_dim
    )
    return plan_layout(polygon, spec), mask.info, stats


# ---------------------------------------------------------------------------
# The repair ladder
# ---------------------------------------------------------------------------


def _adjust_for_execution(spec: KeyHolderSpec) -> tuple[KeyHolderSpec | None, str]:
    """The kernel refused the profile: give it fewer, cleaner vertices."""
    simplify = min(1.0, spec.simplify_mm + 0.35)
    close = min(3.0, spec.close_gaps_mm + 0.4)
    if simplify == spec.simplify_mm and close == spec.close_gaps_mm:
        return None, ""
    return (
        replace(spec, simplify_mm=simplify, close_gaps_mm=close),
        f"simplified the outline to {simplify:g} mm to get it past the kernel",
    )


def _adjust_for_report(
    spec: KeyHolderSpec, failures: list[str]
) -> tuple[KeyHolderSpec | None, str]:
    """Map the validator's hard failures onto the one parameter that fixes them."""
    ids = set(failures)

    if ids & {
        "printability.min_wall",
        "printability.min_feature",
        "printability.feature_size",
        "printability.wall_thickness",
        # The tier-3 rule says the same thing in the category's own words.
        "key_holder.hook_root",
    }:
        thicker = min(8.0, spec.min_feature_mm + 0.8)
        if thicker > spec.min_feature_mm:
            return (
                replace(spec, min_feature_mm=thicker),
                f"raised the minimum feature size to {thicker:g} mm and re-opened "
                "the outline",
            )

    if ids & {"topology.solid_count", "topology.stray_shards", "key_holder.one_piece"}:
        close = min(4.0, spec.close_gaps_mm + 0.8)
        overlap = min(20.0, spec.rail_overlap_mm + 3.0)
        if close > spec.close_gaps_mm or overlap > spec.rail_overlap_mm:
            return (
                replace(spec, close_gaps_mm=close, rail_overlap_mm=overlap),
                f"closed gaps up to {close:g} mm and pushed the rail "
                f"{overlap:g} mm into the silhouette so the piece is one solid",
            )

    if ids & {"topology.self_intersection", "topology.watertight", "topology.winding"}:
        return _adjust_for_execution(spec)

    return None, ""


# ---------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------


def build_key_holder(
    source: str | Path | bytes,
    spec: KeyHolderSpec | None = None,
    *,
    out_dir: Path | str | None = None,
    sandbox: GeometrySandbox | None = None,
    threshold: float | None = None,
    invert: bool | None = None,
    max_dim: int = DEFAULT_MAX_DIM,
    max_attempts: int = MAX_ATTEMPTS,
    render: bool = True,
    views: tuple[str, ...] = STANDARD_VIEWS,
    source_name: str = "an uploaded image",
    on_event: ProgressCallback | None = None,
) -> KeyHolderResult:
    """Trace, lay out, build, validate and render a key holder."""
    started = time.perf_counter()
    spec = spec or KeyHolderSpec()
    sandbox = sandbox or GeometrySandbox(keep_workdir=True)
    store_dir = Path(out_dir) if out_dir else None

    result = GenerationResult(
        model_id=uuid.uuid4().hex,
        status="failed",
        prompt=f"key holder traced from {source_name}",
        route="image_trace",
        language="build123d",
    )
    outcome = KeyHolderResult(result=result)

    def emit_event(phase: str, ok: bool, message: str, **payload: Any) -> None:
        event = LoopEvent(
            step=len(result.events) + 1, phase=phase, ok=ok, message=message, payload=payload
        )
        result.events.append(event)
        if on_event:
            on_event(event)

    # -- trace ------------------------------------------------------------
    step_started = time.perf_counter()
    polygon, mask, trace_stats = trace_image(
        source, threshold=threshold, invert=invert, max_dim=max_dim
    )
    outcome.mask_info = mask.info
    outcome.trace_stats = trace_stats
    result.intent = {
        "category": CATEGORY,
        "subject": "wall key holder",
        "printer_profile": spec.profile_id,
        "material": spec.material,
        "image": mask.info.as_dict(),
        "trace": trace_stats.as_dict(),
    }
    emit_event(
        "trace",
        True,
        f"traced {trace_stats.points_after} outline points from "
        f"{mask.info.traced_size[0]}x{mask.info.traced_size[1]} px "
        f"({mask.info.source})",
        duration_ms=int((time.perf_counter() - step_started) * 1000),
        **trace_stats.as_dict(),
    )

    attempt_spec = spec
    for attempt in range(1, max_attempts + 1):
        outcome.attempts = attempt
        result.iterations = attempt

        # -- lay out and emit ---------------------------------------------
        try:
            layout = plan_layout(polygon, attempt_spec)
        except DesignError as exc:
            emit_event("codegen", False, str(exc))
            result.status = "failed"
            result.message = str(exc)
            break

        outcome.plan = layout
        script = emit(layout, source_name=source_name)
        result.source_code = script
        result.params = _exposed_params(layout)
        result.exposed_params = param_schema(layout)
        emit_event(
            "codegen",
            True,
            f"laid out {layout.summary()}",
            **layout.as_dict(),
        )

        # -- execute --------------------------------------------------------
        execution = sandbox.execute(
            ExecuteRequest(
                source=script,
                language="build123d",
                params=result.params,
                metadata={
                    "generator": "FormForge key holder",
                    "units": "millimeter",
                    "category": CATEGORY,
                },
                limits=Limits(),
                tessellation=Tessellation(),
                # The script is emitted by this module, not written by a model:
                # its outline is a literal point list by design, which is exactly
                # what the magic-number rule exists to prevent a model from doing.
                enforce_named_constants=False,
            )
        )
        if not execution.ok:
            emit_event(
                "execute",
                False,
                f"{execution.error_class}: {execution.message}",
                hint=execution.hint,
            )
            result.message = f"{execution.error_class}: {execution.message}"
            adjusted, note = _adjust_for_execution(attempt_spec)
            if adjusted is None or attempt == max_attempts:
                result.status = "failed"
                break
            outcome.adjustments.append(note)
            emit_event("codegen", True, f"retrying: {note}")
            attempt_spec = adjusted
            continue

        result.artifacts = dict(execution.artifacts)
        result.stats = dict(execution.stats)
        result.workdir = execution.workdir
        emit_event(
            "execute",
            True,
            f"solid built: {execution.stats.get('triangles', 0)} triangles",
            **{k: v for k, v in execution.stats.items() if k != "brep_features"},
        )

        # -- validate -------------------------------------------------------
        report = validate(
            execution.artifacts["stl"],
            profile_id=attempt_spec.profile_id,
            material=attempt_spec.material,
            category=CATEGORY,
            params=result.params,
            template_invariants=None,
            expected_solids=1,
            brep_features=execution.stats.get("brep_features"),
            # The validator names axes by the bounding box: width is X, depth
            # is Y, height is Z. This part is modelled lying in its print
            # orientation, so the height *of the piece on the wall* is Y, and
            # Z is how far it stands off the wall. Passing the wall height as
            # "height_mm" would compare it against the hook projection.
            requested_dimensions={
                "width_mm": layout.size_mm[0],
                "depth_mm": layout.size_mm[1],
            },
        )
        result.validation = report.as_dict()
        emit_event(
            "validate",
            report.passed,
            report.summary_line(),
            failures=[c.id for c in report.hard_failures],
            warnings=[c.id for c in report.warnings],
        )

        if not report.passed:
            adjusted, note = _adjust_for_report(
                attempt_spec, [c.id for c in report.hard_failures]
            )
            result.message = "; ".join(c.message for c in report.hard_failures[:2])
            if adjusted is None or attempt == max_attempts:
                result.status = "failed"
                break
            outcome.adjustments.append(note)
            emit_event("codegen", True, f"retrying: {note}")
            attempt_spec = adjusted
            continue

        result.status = "ok"
        break

    # -- render -----------------------------------------------------------
    preview_dir = (store_dir or Path(".")) / result.model_id / "previews"
    if render and outcome.plan is not None:
        # Drawn whether or not the build succeeded, and drawn first: when a key
        # holder comes out wrong the reason is almost always in what the tracer
        # decided, and this is the only picture of that decision.
        from .preview import render_trace  # noqa: PLC0415

        try:
            preview_dir.mkdir(parents=True, exist_ok=True)
            result.previews["trace"] = str(
                render_trace(outcome.plan, preview_dir / "preview_trace.png")
            )
            emit_event("render", True, "drew the traced outline")
        except Exception as exc:  # noqa: BLE001
            emit_event("render", False, f"the trace preview could not be drawn: {exc}")

    if result.status == "ok" and render and result.artifacts.get("stl"):
        try:
            previews = render_views(result.artifacts["stl"], preview_dir, views=views)
            result.previews.update(previews.views)
            if previews.contact_sheet:
                result.previews["sheet"] = previews.contact_sheet
            emit_event("render", True, f"{len(previews.views)} views rendered")
        except Exception as exc:  # noqa: BLE001
            # A render failure is a missing picture, not a broken model. The
            # STL is already validated; refusing to hand it over because the
            # preview did not draw would be the wrong trade.
            emit_event("render", False, f"previews could not be rendered: {exc}")

    if outcome.plan is not None:
        result.stats["keyholder"] = outcome.plan.as_dict()
    result.duration_ms = int((time.perf_counter() - started) * 1000)
    if result.status == "ok" and not result.message:
        result.message = outcome.summary()
    return outcome


def param_schema(layout: KeyHolderPlan) -> dict[str, Any]:
    """A JSON Schema `properties` block for the adjustable numbers.

    The same contract a template's `param_schema` gives the UI and `params.json`:
    every value with the range it was checked against, so a slider cannot ask
    for a key holder nobody has built.
    """
    spec = layout.spec
    properties: dict[str, Any] = {}
    for name, (low, high) in PARAM_RANGES.items():
        value = getattr(spec, name, None)
        if value is None:
            continue
        properties[name] = {
            "type": (
                "integer" if isinstance(value, int) and name == "hook_count" else "number"
            ),
            "minimum": low,
            "maximum": high,
            "default": value,
        }
    properties["detail"] = {
        "type": "string",
        "enum": list(DETAIL_MODES),
        "default": spec.detail,
    }
    properties["mount"] = {"type": "string", "enum": list(MOUNT_MODES), "default": layout.mount}
    properties["rail"] = {"type": "boolean", "default": spec.rail}
    return properties


def _exposed_params(layout: KeyHolderPlan) -> dict[str, Any]:
    """The parameters worth showing a user, with the outline left out.

    `params.json` is meant to be read and edited. Three hundred traced vertices
    in it would bury the eleven numbers that are actually adjustable.
    """
    spec = layout.spec
    return {
        "width_mm": round(layout.size_mm[0], 2),
        "height_mm": round(layout.size_mm[1], 2),
        "plaque_t_mm": layout.plaque_t_mm,
        "hook_count": len(layout.hook_x_mm),
        "hook_out_mm": spec.hook_out_mm,
        "hook_w_mm": spec.hook_w_mm,
        "hook_root_h_mm": spec.hook_root_h_mm,
        "hook_lip_mm": spec.hook_lip_mm,
        "rail_h_mm": spec.rail_h_mm if spec.rail else 0.0,
        "min_feature_mm": spec.min_feature_mm,
        "detail": spec.detail,
        "engrave_depth_mm": spec.engrave_depth_mm,
        "mount": layout.mount,
        "screw_d_mm": spec.screw_d_mm,
        "printer_profile": spec.profile_id or DEFAULT_PROFILE_ID,
        "material": spec.material,
    }
