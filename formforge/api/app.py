"""Public HTTP API (spec sections 2 and 12).

    POST   /v1/generate              -> 202 {job_id, model_id}
    GET    /v1/models/{id}           -> the model record
    GET    /v1/models/{id}/status    -> {status, step, phase, progress}
    WS     /v1/models/{id}/stream    -> live agent-loop events
    POST   /v1/models/{id}/modify    -> 202 (a new child model)
    POST   /v1/models/{id}/slice     -> 202
    GET    /v1/models/{id}/download  -> the artifact
    GET    /v1/templates             -> the registry
    GET    /v1/templates/{id}        -> detail and parameter schema
    POST   /v1/feedback              -> a print outcome
    GET    /v1/profiles              -> printer profiles

Generation is asynchronous because it takes seconds to minutes, and holding an
HTTP connection open for that is how you discover every proxy's idle timeout.
The job is submitted, an id comes back immediately, and the WebSocket carries
the loop.

The WebSocket is not a progress bar. It replays every step of the agent loop,
and the reason is in spec section 12: watching the system find a 1.1 mm wall,
thicken it and regenerate is both reassuring and the clearest possible argument
for why this is worth paying for. Hiding that behind a spinner throws away the
best demo the product has.

**Startup refuses untrusted traffic when the sandbox does not isolate the host
kernel.** The development runtime executes model-authored Python directly; that
is fine on a laptop and a serious incident in production, so the check is code
rather than a paragraph in a runbook.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("formforge.api")

try:
    from fastapi import (
        BackgroundTasks,
        FastAPI,
        HTTPException,
        Query,
        WebSocket,
        WebSocketDisconnect,
    )
    from fastapi.responses import FileResponse, JSONResponse
    from pydantic import BaseModel, Field

    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    FASTAPI_AVAILABLE = False
    BaseModel = object  # type: ignore[assignment,misc]

from ..bundle import write_bundle
from ..dfm import DEFAULT_PROFILE_ID, PROFILES
from ..llm import build_client
from ..orchestrator import Orchestrator
from ..registry import TemplateRegistry
from ..sandbox import GeometrySandbox
from ..slicer import slice_model

STORE_DIR = Path(os.environ.get("FORMFORGE_STORE", Path.home() / ".formforge" / "models"))

# How many events to retain per job for a client that connects late. A loop
# emits well under this, so a client that connects after the run finished still
# gets the whole story.
EVENT_BUFFER = 256


@dataclass
class Job:
    """One in-flight or finished generation."""

    job_id: str
    model_id: str
    status: str = "queued"
    phase: str = "queued"
    step: int = 0
    events: deque = field(default_factory=lambda: deque(maxlen=EVENT_BUFFER))
    result: Any = None
    error: str = ""
    parent_id: str | None = None

    @property
    def progress(self) -> float:
        """Rough fraction complete, by phase.

        Deliberately coarse. A generation that repairs itself goes backwards
        through the phases, and a progress bar that jumps back is worse than one
        that is approximate -- the phase name is the honest signal, and the
        number is there for a UI that needs one.
        """
        order = ["queued", "policy", "intent", "route", "codegen", "execute",
                 "validate", "render", "critique", "done"]
        try:
            return round(order.index(self.phase) / (len(order) - 1), 2)
        except ValueError:
            return 0.0

    def as_status(self) -> dict:
        return {
            "job_id": self.job_id,
            "model_id": self.model_id,
            "status": self.status,
            "phase": self.phase,
            "step": self.step,
            "progress": self.progress,
            "error": self.error,
        }


class JobStore:
    """In-memory job registry with per-job event fan-out.

    In production this is Redis plus Postgres (spec section 11); the interface
    is the same and the swap is confined to this class. Keeping it in-process
    here means the API runs with no infrastructure, which is what makes it
    testable.
    """

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.by_model: dict[str, Job] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)

    def create(self, parent_id: str | None = None) -> Job:
        job = Job(job_id=uuid.uuid4().hex, model_id=uuid.uuid4().hex, parent_id=parent_id)
        self.jobs[job.job_id] = job
        self.by_model[job.model_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def for_model(self, model_id: str) -> Job | None:
        return self.by_model.get(model_id)

    def publish(self, job: Job, event: dict) -> None:
        job.events.append(event)
        for queue in list(self._subscribers.get(job.job_id, [])):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A client too slow to keep up loses events rather than
                # stalling the generation that is producing them.
                pass

    def subscribe(self, job: Job) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=EVENT_BUFFER)
        for event in job.events:
            queue.put_nowait(event)
        self._subscribers[job.job_id].append(queue)
        return queue

    def unsubscribe(self, job: Job, queue: asyncio.Queue) -> None:
        subscribers = self._subscribers.get(job.job_id, [])
        if queue in subscribers:
            subscribers.remove(queue)


if FASTAPI_AVAILABLE:

    class GenerateRequest(BaseModel):
        prompt: str = Field(..., min_length=1, max_length=2000)
        printer_profile: str = DEFAULT_PROFILE_ID
        material: str = "PLA"
        constraints: dict[str, Any] = Field(default_factory=dict)
        interactive: bool = True

    class ModifyRequest(BaseModel):
        param_changes: dict[str, Any]

    class SliceRequest(BaseModel):
        printer_profile: str | None = None
        quality: str = "standard"
        supports: bool = False

    class FeedbackRequest(BaseModel):
        """A print outcome (spec section 11).

        The only ground truth that exists for whether any of this works, and the
        dataset that eventually lets the DFM constants be tuned from evidence
        rather than folklore. Worth collecting from day one even though nothing
        consumes it yet -- it cannot be collected retroactively.
        """

        model_id: str
        printed: bool
        success: bool | None = None
        printer: str | None = None
        material: str | None = None
        issues: list[str] = Field(default_factory=list)
        notes: str | None = None


def create_app(
    *,
    registry: TemplateRegistry | None = None,
    orchestrator: Orchestrator | None = None,
    store_dir: Path | None = None,
    allow_unsafe_sandbox: bool = False,
):
    """Build the FastAPI application."""
    if not FASTAPI_AVAILABLE:
        raise RuntimeError(
            "FastAPI is not installed. Install it with `pip install "
            "'formforge[api]'` to run the HTTP gateway."
        )

    store = Path(store_dir or STORE_DIR)
    store.mkdir(parents=True, exist_ok=True)
    templates = registry or TemplateRegistry.load(strict=False)
    sandbox = GeometrySandbox(keep_workdir=True)

    if not sandbox.production_ready() and not allow_unsafe_sandbox:
        if os.environ.get("FORMFORGE_ALLOW_UNSAFE_SANDBOX") != "1":
            raise RuntimeError(
                "The geometry sandbox runtime is "
                f"{sandbox.runtime!r}, which does not isolate the host kernel. "
                "This API executes model-authored Python, so serving untrusted "
                "traffic on it is a remote-code-execution risk. Set "
                "FORMFORGE_SANDBOX_RUNTIME=gvisor, or "
                "FORMFORGE_ALLOW_UNSAFE_SANDBOX=1 if you accept the risk for a "
                "local development server."
            )
        log.warning(
            "serving with sandbox runtime %r, which does not isolate the host "
            "kernel; do not expose this to untrusted traffic",
            sandbox.runtime,
        )

    engine = orchestrator or Orchestrator(
        registry=templates,
        client=build_client(),
        sandbox=sandbox,
        output_dir=store,
    )
    jobs = JobStore()

    app = FastAPI(
        title="FormForge",
        version="0.1.0",
        description=(
            "Natural language to print-ready STL/3MF, via a parametric CAD "
            "kernel rather than a mesh generator."
        ),
    )

    # -- generation ----------------------------------------------------
    @app.post("/v1/generate", status_code=202)
    async def generate(request: GenerateRequest, background: BackgroundTasks):
        job = jobs.create()
        background.add_task(_run_generation, job, request)
        return {"job_id": job.job_id, "model_id": job.model_id, "status": "queued"}

    async def _run_generation(job: Job, request: GenerateRequest) -> None:
        loop = asyncio.get_running_loop()

        def on_event(event) -> None:
            job.phase = event.phase
            job.step = event.step
            payload = event.as_dict()
            # The orchestrator runs in a worker thread; hop back to the loop
            # thread before touching the asyncio queues.
            loop.call_soon_threadsafe(jobs.publish, job, payload)

        job.status = "running"
        try:
            result = await asyncio.to_thread(
                engine.generate,
                request.prompt,
                printer_profile=request.printer_profile,
                material=request.material,
                interactive=request.interactive,
                on_event=on_event,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("generation %s failed", job.job_id)
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            jobs.publish(job, {"phase": "failed", "ok": False, "message": job.error})
            return

        # The orchestrator assigns its own model id; keep the one the client
        # already has so the URLs it was handed keep working.
        result.model_id = job.model_id
        job.result = result
        job.status = result.status
        job.phase = "done"

        if result.status == "ok":
            template = (
                templates.get(result.template_id)
                if result.template_id and result.template_id in templates
                else None
            )
            bundle = await asyncio.to_thread(
                write_bundle, result, store / result.model_id / "bundle", template=template
            )
            result.artifacts.update(bundle.files)

        jobs.publish(
            job,
            {"phase": "done", "ok": result.ok, "message": result.message, "status": result.status},
        )

    @app.get("/v1/models/{model_id}")
    async def get_model(model_id: str):
        job = _require_model(model_id)
        if job.result is None:
            return JSONResponse(job.as_status(), status_code=202)
        return job.result.as_dict()

    @app.get("/v1/models/{model_id}/status")
    async def get_status(model_id: str):
        return _require_model(model_id).as_status()

    @app.websocket("/v1/models/{model_id}/stream")
    async def stream(websocket: WebSocket, model_id: str):
        await websocket.accept()
        job = jobs.for_model(model_id)
        if job is None:
            await websocket.send_json({"error": f"no model {model_id}"})
            await websocket.close()
            return

        queue = jobs.subscribe(job)
        try:
            while True:
                if job.status in {"ok", "failed", "refused", "needs_clarification"} and queue.empty():
                    await websocket.send_json({"phase": "closed", "status": job.status})
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    # A keepalive, so an idle proxy does not drop a connection
                    # during a long geometry step.
                    await websocket.send_json({"phase": "keepalive"})
                    continue
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        finally:
            jobs.unsubscribe(job, queue)

    @app.post("/v1/models/{model_id}/modify", status_code=202)
    async def modify(model_id: str, request: ModifyRequest, background: BackgroundTasks):
        parent = _require_model(model_id)
        if parent.result is None or not parent.result.template_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "this model was not built from a template, so it has no "
                    "parameter schema to modify; regenerate from source instead"
                ),
            )
        child = jobs.create(parent_id=model_id)
        template_id = parent.result.template_id
        merged = {**(parent.result.params or {}), **request.param_changes}

        async def run() -> None:
            child.status = "running"
            result = await asyncio.to_thread(
                engine.generate,
                prompt=parent.result.prompt,
                printer_profile=parent.result.intent.get("printer_profile", DEFAULT_PROFILE_ID),
                material=parent.result.intent.get("material", "PLA"),
                interactive=False,
                template_id=template_id,
                params=merged,
            )
            result.model_id = child.model_id
            child.result = result
            child.status = result.status
            child.phase = "done"

        background.add_task(run)
        return {"job_id": child.job_id, "model_id": child.model_id, "parent_id": model_id}

    @app.post("/v1/models/{model_id}/slice", status_code=202)
    async def slice_endpoint(model_id: str, request: SliceRequest, background: BackgroundTasks):
        job = _require_result(model_id)
        source = job.result.artifacts.get("3mf") or job.result.artifacts.get("stl")
        if not source:
            raise HTTPException(status_code=400, detail="this model has no mesh to slice")

        async def run() -> None:
            summary = await asyncio.to_thread(
                slice_model,
                source,
                profile_id=request.printer_profile
                or job.result.intent.get("printer_profile", DEFAULT_PROFILE_ID),
                quality=request.quality,
                supports=request.supports,
                out_dir=store / model_id,
            )
            job.result.stats["slice_summary"] = summary.as_dict()

        background.add_task(run)
        return {"job_id": job.job_id, "status": "queued"}

    @app.get("/v1/models/{model_id}/download")
    async def download(
        model_id: str,
        format: str = Query("3mf", pattern="^(3mf|stl|step|source|params|report)$"),
    ):
        job = _require_result(model_id)
        path = job.result.artifacts.get(format)
        if not path or not Path(path).exists():
            raise HTTPException(
                status_code=404,
                detail=(
                    f"this model has no {format} artifact. The STEP export is "
                    "only produced on the parametric path."
                ),
            )
        filename = {
            "3mf": "model.3mf",
            "stl": "model.stl",
            "step": "model.step",
            "source": "source.py",
            "params": "params.json",
            "report": "report.json",
        }[format]
        return FileResponse(path, filename=filename)

    # -- catalogue -----------------------------------------------------
    @app.get("/v1/templates")
    async def list_templates(category: str | None = None, q: str | None = None):
        if q:
            return {"templates": [m.as_dict() for m in templates.search(q, category, limit=10)]}
        return {"templates": [t.summary() for t in templates.list(category)]}

    @app.get("/v1/templates/{template_id}")
    async def get_template(template_id: str):
        try:
            return templates.get(template_id).detail()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.get("/v1/profiles")
    async def list_profiles():
        return {
            "default": DEFAULT_PROFILE_ID,
            "profiles": [
                {
                    "id": p.id,
                    "display_name": p.display_name,
                    "nozzle_mm": p.nozzle_mm,
                    "layer_mm": p.layer_mm,
                    "build_volume_mm": list(p.build_volume_mm),
                }
                for p in PROFILES.values()
            ],
        }

    @app.post("/v1/feedback", status_code=201)
    async def feedback(request: FeedbackRequest):
        record = request.model_dump()
        path = store / "print_feedback.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        return {"recorded": True}

    @app.get("/healthz")
    async def health():
        return {
            "ok": True,
            "templates": len(templates),
            "sandbox": sandbox.describe(),
            "model_client": engine.client.available,
        }

    # -- helpers -------------------------------------------------------
    def _require_model(model_id: str) -> Job:
        job = jobs.for_model(model_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no model {model_id}")
        return job

    def _require_result(model_id: str) -> Job:
        job = _require_model(model_id)
        if job.result is None:
            raise HTTPException(
                status_code=409,
                detail=f"model {model_id} is still {job.status}; poll /status first",
            )
        return job

    return app


app = None
if FASTAPI_AVAILABLE and os.environ.get("FORMFORGE_AUTO_APP") == "1":  # pragma: no cover
    app = create_app()
