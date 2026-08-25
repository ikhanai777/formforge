"""The agent loop (spec section 5).

    intent -> template match -> param fill / codegen -> execute -> validate
    -> render -> critique -> revise, up to N times

Three decisions define the behaviour of this module, and they are all about
knowing when to stop:

* **A bounded iteration budget.** Four attempts, then a graceful partial result
  with an explanation. An agent loop with no ceiling will happily spend a
  hundred dollars discovering that a request is impossible.
* **One escalation, not a ladder.** If the standard tier has failed three times,
  the problem is not more attempts at that tier -- it is a harder model. The
  whole conversation escalates once, gets one attempt, and then the loop ends.
* **Failure is reported, not hidden.** A run that exhausts its budget returns
  the best artifact it produced along with what was wrong. Returning nothing
  wastes the work; returning it silently as a success is worse.

Every step emits an event, so the whole loop can be streamed to the user (spec
section 12). Watching "checking wall thickness... found 1.1 mm at the drainage
boss, thickening to 2.4 mm and regenerating" is a better demonstration of why
this is worth paying for than any amount of marketing copy, and it costs one
callback to support.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import policy
from ..dfm import DEFAULT_PROFILE_ID
from ..llm import Tier, Usage, build_client
from ..registry import Route, TemplateRegistry
from ..render import CRITIQUE_VIEWS, render_views
from ..sandbox import ExecuteRequest, GeometrySandbox, Limits, Tessellation
from ..validation import ValidationReport, validate
from . import codegen
# Imported by name, not as modules: the package __init__ binds `critique` and
# `intent` to a function and a class, which would shadow the modules here.
from .critique import CritiqueResult, critique as run_critique
from .intent import parse as parse_intent

# Attempts before giving up (spec section 5.2). Iteration 4 runs on the
# escalated tier.
MAX_ITERATIONS = 4
# Iteration at which the whole conversation moves to the escalated model.
ESCALATE_AFTER = 3


@dataclass
class LoopEvent:
    """One step of the loop, for the progress stream and the event log."""

    step: int
    phase: str
    ok: bool
    message: str
    payload: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0

    def as_dict(self) -> dict:
        return {
            "step": self.step,
            "phase": self.phase,
            "ok": self.ok,
            "message": self.message,
            "payload": self.payload,
            "duration_ms": self.duration_ms,
        }


@dataclass
class GenerationResult:
    """Everything one generation produced, successful or not."""

    model_id: str
    status: str  # ok | failed | refused | needs_clarification
    prompt: str
    intent: dict[str, Any] = field(default_factory=dict)
    template_id: str | None = None
    route: str = "freeform"
    source_code: str = ""
    language: str = "build123d"
    params: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    previews: dict[str, str] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] | None = None
    critique: dict[str, Any] | None = None
    clarifications: list[dict[str, Any]] = field(default_factory=list)
    events: list[LoopEvent] = field(default_factory=list)
    iterations: int = 0
    usage: Usage = field(default_factory=Usage)
    duration_ms: int = 0
    message: str = ""
    workdir: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "status": self.status,
            "prompt": self.prompt,
            "intent": self.intent,
            "template_id": self.template_id,
            "route": self.route,
            "language": self.language,
            "params": self.params,
            "artifacts": self.artifacts,
            "previews": self.previews,
            "stats": self.stats,
            "validation": self.validation,
            "critique": self.critique,
            "clarifications": self.clarifications,
            "iterations": self.iterations,
            "usage": self.usage.as_dict(),
            "duration_ms": self.duration_ms,
            "message": self.message,
            "events": [e.as_dict() for e in self.events],
        }

    def summary(self) -> str:
        """A human-readable account of what happened."""
        if self.status == "needs_clarification":
            questions = "\n".join(f"  - {c['question']}" for c in self.clarifications)
            return f"Need one thing before I can build this:\n{questions}"
        if self.status == "refused":
            return self.message
        if not self.ok:
            return f"Generation failed after {self.iterations} attempt(s): {self.message}"

        bbox = self.stats.get("bbox_mm") or []
        size = " x ".join(f"{v:.0f}" for v in bbox) + " mm" if bbox else "unknown size"
        lines = [
            f"Built {self.intent.get('subject') or 'model'} -- {size}, "
            f"{self.stats.get('triangles', 0)} triangles, "
            f"{self.iterations} iteration(s), {self.duration_ms / 1000:.1f}s."
        ]
        if self.template_id:
            lines.append(f"Template: {self.template_id}")
        report = self.validation or {}
        summary = report.get("summary") or {}
        if summary.get("warnings"):
            lines.append(f"{summary['warnings']} warning(s) -- see the report.")
        return "\n".join(lines)


ProgressCallback = Callable[[LoopEvent], None]


class Orchestrator:
    """Runs the generate-validate-critique-revise cycle."""

    def __init__(
        self,
        registry: TemplateRegistry | None = None,
        client=None,
        sandbox: GeometrySandbox | None = None,
        *,
        output_dir: Path | str | None = None,
        max_iterations: int = MAX_ITERATIONS,
        enable_critique: bool = True,
    ):
        self.registry = registry if registry is not None else TemplateRegistry.load(strict=False)
        self.client = client if client is not None else build_client()
        self.sandbox = sandbox or GeometrySandbox(keep_workdir=True)
        self.output_dir = Path(output_dir) if output_dir else None
        self.max_iterations = max_iterations
        self.enable_critique = enable_critique

    # -- entry point -------------------------------------------------------
    def generate(
        self,
        prompt: str,
        *,
        printer_profile: str = DEFAULT_PROFILE_ID,
        material: str = "PLA",
        interactive: bool = True,
        template_id: str | None = None,
        params: dict[str, Any] | None = None,
        on_event: ProgressCallback | None = None,
    ) -> GenerationResult:
        """Run one generation end to end."""
        started = time.perf_counter()
        model_id = uuid.uuid4().hex
        result = GenerationResult(model_id=model_id, status="failed", prompt=prompt)

        def emit(phase: str, ok: bool, message: str, **payload: Any) -> None:
            event = LoopEvent(
                step=len(result.events) + 1,
                phase=phase,
                ok=ok,
                message=message,
                payload=payload,
            )
            result.events.append(event)
            if on_event:
                on_event(event)

        # -- 0. content policy, before anything is generated ---------------
        screening = policy.screen(prompt, self.client)
        if not screening.allowed:
            emit("policy", False, screening.user_message(), **screening.as_dict())
            result.status = "refused"
            result.message = screening.user_message()
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            return result
        if screening.decision is policy.Decision.FLAG:
            emit("policy", True, "request flagged for review", **screening.as_dict())

        # -- 1. intent ------------------------------------------------------
        parsed = parse_intent(
            prompt,
            self.client,
            printer_profile=printer_profile,
            material=material,
            interactive=interactive,
        )
        result.intent = parsed.as_dict()
        emit("intent", True, parsed.summary(), **parsed.as_dict())

        if parsed.needs_clarification and interactive and not template_id:
            result.status = "needs_clarification"
            result.clarifications = parsed.clarifications
            result.message = "a functional dimension is missing"
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            emit("clarify", True, result.message, questions=parsed.clarifications)
            return result

        # -- 2. route -------------------------------------------------------
        route, match = self._route(parsed, template_id)
        template = match.template if match else None
        result.route = route.value
        result.template_id = template.id if template and route is Route.TEMPLATE else None
        emit(
            "route",
            True,
            f"{route.value}"
            + (f" via {template.id} (score {match.score:.2f})" if match else ""),
            route=route.value,
            template_id=template.id if template else None,
            score=round(match.score, 4) if match else 0.0,
        )

        # -- 3-6. the iteration loop ---------------------------------------
        return self._iterate(
            result,
            parsed,
            route,
            template,
            params,
            started,
            emit,
        )

    # -- routing -----------------------------------------------------------
    def _route(self, parsed, template_id: str | None):
        """Pick the generation path (spec section 6.2)."""
        if template_id:
            if template_id not in self.registry:
                raise KeyError(f"no template {template_id!r}")
            template = self.registry.get(template_id)
            from ..registry import Match  # noqa: PLC0415

            return Route.TEMPLATE, Match(template, 1.0, Route.TEMPLATE)
        return self.registry.route(
            parsed.search_query(),
            parsed.category,
            requires_text=bool(parsed.text_content),
        )

    # -- the loop ----------------------------------------------------------
    def _iterate(
        self,
        result: GenerationResult,
        parsed,
        route: Route,
        template,
        params: dict[str, Any] | None,
        started: float,
        emit,
    ) -> GenerationResult:
        failures: list[str] = []
        best_report: ValidationReport | None = None

        for iteration in range(1, self.max_iterations + 1):
            result.iterations = iteration
            tier = Tier.ESCALATED if iteration > ESCALATE_AFTER else Tier.STANDARD
            if iteration > ESCALATE_AFTER:
                emit(
                    "escalate",
                    True,
                    f"three attempts failed; escalating to the {tier.value} model "
                    "for one final attempt",
                )

            # -- 3. produce a script ---------------------------------------
            step_started = time.perf_counter()
            generated = self._produce(
                parsed, route, template, params, failures, tier, iteration
            )
            if not generated.ok:
                emit("codegen", False, generated.error)
                result.message = generated.error
                break

            result.source_code = generated.source
            result.language = generated.language
            result.params = generated.params or generated.exposed_params
            emit(
                "codegen",
                True,
                generated.notes or f"script generated ({len(generated.source)} chars)",
                duration_ms=int((time.perf_counter() - step_started) * 1000),
            )

            # -- 4. execute -------------------------------------------------
            execution = self.sandbox.execute(
                ExecuteRequest(
                    source=generated.source,
                    language=generated.language,
                    params=generated.params,
                    metadata=self._metadata(parsed, template),
                    limits=Limits(),
                    tessellation=Tessellation(),
                    # Registry templates are human-reviewed, so the
                    # magic-number style rule applies only to model output.
                    enforce_named_constants=route is not Route.TEMPLATE,
                )
            )
            if not execution.ok:
                emit(
                    "execute",
                    False,
                    f"{execution.error_class}: {execution.message}",
                    hint=execution.hint,
                )
                failures.append(execution.feedback())
                result.message = f"{execution.error_class}: {execution.message}"
                continue

            result.artifacts = dict(execution.artifacts)
            result.stats = dict(execution.stats)
            result.workdir = execution.workdir
            emit(
                "execute",
                True,
                f"solid built: {_describe_bbox(execution.stats)}, "
                f"{execution.stats.get('triangles', 0)} triangles",
                **{k: v for k, v in execution.stats.items() if k != "brep_features"},
            )

            # -- 5. validate ------------------------------------------------
            report = validate(
                execution.artifacts["stl"],
                profile_id=parsed.printer_profile,
                material=parsed.material,
                category=parsed.category,
                params=generated.params,
                template_invariants=template.invariants if template else None,
                expected_solids=template.expected_solids if template else 1,
                brep_features=execution.stats.get("brep_features"),
                text_features=(
                    template.resolve_text_features(generated.params) if template else None
                ),
                requested_dimensions=self._requested_dimensions(parsed, template, generated),
            )
            result.validation = report.as_dict()
            best_report = report
            emit(
                "validate",
                report.passed,
                report.summary_line(),
                failures=[c.id for c in report.hard_failures],
                warnings=[c.id for c in report.warnings],
            )

            if not report.passed:
                failures.append(report.agent_feedback())
                result.message = "; ".join(c.message for c in report.hard_failures[:2])
                if route is Route.TEMPLATE:
                    # A registry template that fails its own checks is a
                    # registry bug, not something a retry will fix: the same
                    # parameters produce the same solid every time.
                    emit(
                        "abort",
                        False,
                        "the template failed its own validation, which retrying "
                        "cannot change; this is a registry defect",
                    )
                    break
                continue

            # -- 6. render and critique -------------------------------------
            previews = self._render(result, execution)
            emit("render", True, f"{len(previews.views)} views rendered")
            result.previews = dict(previews.views)
            if previews.contact_sheet:
                result.previews["sheet"] = previews.contact_sheet

            verdict = self._critique(previews, parsed, report)
            result.critique = verdict.as_dict()
            emit("critique", verdict.passed, verdict.summary or verdict.verdict, **verdict.as_dict())

            if verdict.verdict == "fail" or (
                verdict.verdict == "revise" and verdict.major_issues
            ):
                if route is Route.TEMPLATE:
                    # The geometry is verified; the critique is telling us the
                    # template was the wrong choice, not that it is broken.
                    # Ship it with the note rather than loop pointlessly.
                    result.message = verdict.summary
                else:
                    failures.append(verdict.agent_feedback())
                    result.message = verdict.summary
                    continue

            result.status = "ok"
            result.message = verdict.summary or "generated and validated"
            break

        if result.status != "ok":
            result.message = result.message or "generation did not converge"
            if best_report is not None and result.validation is None:
                result.validation = best_report.as_dict()
            emit("failed", False, result.message)

        result.usage = getattr(self.client, "total", Usage())
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result

    # -- step helpers ------------------------------------------------------
    def _produce(
        self,
        parsed,
        route: Route,
        template,
        params: dict[str, Any] | None,
        failures: list[str],
        tier: Tier,
        iteration: int,
    ) -> codegen.CodegenResult:
        """Produce a script for this iteration."""
        if route is Route.TEMPLATE and template is not None:
            if params:
                merged = template.merge_params(params)
                return codegen.CodegenResult(
                    source=template.render_source(merged),
                    params=merged,
                    language=template.language,
                    notes="parameters supplied by the caller",
                )
            return codegen.fill_template_params(template, parsed, self.client)

        exemplars = self._exemplars(parsed, template, route)
        return codegen.generate_freeform(
            parsed,
            self.client,
            exemplars=exemplars,
            failures=failures,
            templates=self.registry.all(),
            tier=tier,
        )

    def _exemplars(self, parsed, template, route: Route) -> list:
        """Few-shot examples for the freeform path.

        A near-miss template is the most valuable thing that can go in the
        prompt: it is verified geometry for a related part, so the model edits
        working code rather than inventing new code (spec section 6.2's
        "template as a starting point" band).
        """
        exemplars = []
        if route is Route.TEMPLATE_SEED and template is not None:
            exemplars.append(template)
        for match in self.registry.search(
            parsed.search_query(),
            parsed.category,
            limit=3,
            requires_text=bool(parsed.text_content),
        ):
            if match.template not in exemplars:
                exemplars.append(match.template)
        return exemplars[:3]

    def _metadata(self, parsed, template) -> dict[str, str]:
        """Metadata embedded in the 3MF, so print settings survive the handoff."""
        metadata = {
            "generator": "FormForge",
            "printer_profile": parsed.printer_profile,
            "material": parsed.material,
            "units": "millimeter",
        }
        if template is not None:
            metadata["template"] = f"{template.id}@{template.version}"
            if template.notes:
                metadata["print_orientation"] = template.notes.strip().splitlines()[0]
        return metadata

    def _requested_dimensions(self, parsed, template, generated) -> dict[str, float]:
        """What the bounding box is checked against.

        A template's own `dimension_map` wins over the parsed intent: the
        template author knows which of its parameters is the overall size and
        which is an internal one, and the parser does not.
        """
        if template is not None:
            return template.requested_dimensions(generated.params)
        return {
            key: value
            for key, value in parsed.dimensions.items()
            if key in {"length_mm", "width_mm", "height_mm", "depth_mm"}
        }

    def _render(self, result: GenerationResult, execution):
        out = (
            Path(self.output_dir) / result.model_id
            if self.output_dir
            else Path(execution.workdir or ".") / "previews"
        )
        return render_views(
            execution.artifacts["stl"],
            out,
            views=CRITIQUE_VIEWS,
            size=512,
            show_build_plate_grid=True,
        )

    def _critique(self, previews, parsed, report: ValidationReport):
        if not self.enable_critique:
            return CritiqueResult(
                ran=False, verdict="pass", summary="visual critique disabled"
            )
        return run_critique(
            previews,
            parsed,
            self.client,
            validation_summary=report.summary_line(),
        )


def _describe_bbox(stats: dict) -> str:
    bbox = stats.get("bbox_mm") or []
    if len(bbox) != 3:
        return "unknown size"
    return " x ".join(f"{v:.1f}" for v in bbox) + " mm"


def generate(prompt: str, **kwargs: Any) -> GenerationResult:
    """One-shot convenience wrapper around a default Orchestrator."""
    return Orchestrator().generate(prompt, **kwargs)
