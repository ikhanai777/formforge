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
        Request,
        WebSocket,
        WebSocketDisconnect,
    )
    from fastapi.responses import FileResponse, JSONResponse
    from pydantic import BaseModel, Field

    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    FASTAPI_AVAILABLE = False
    BaseModel = object  # type: ignore[assignment,misc]

from ..accounts import InsufficientCredits, plans
from ..bundle import write_bundle
from ..dfm import DEFAULT_PROFILE_ID, PROFILES
from ..llm import build_client
from ..orchestrator import Orchestrator
from ..registry import TemplateRegistry
from ..sandbox import GeometrySandbox
from ..slicer import slice_model
from ..storage import LINK_TTL_SECONDS, StorageError, sign_download, verify_download
from ..store import PRINT_ISSUES, Store

STORE_DIR = Path(os.environ.get("FORMFORGE_STORE", Path.home() / ".formforge" / "models"))

# How many events to retain per job for a client that connects late. A loop
# emits well under this, so a client that connects after the run finished still
# gets the whole story.
EVENT_BUFFER = 256

# Download formats mapped to the names the plans use. `3mf` and `stl` are both
# meshes but only STL is on the free tier, so they are not interchangeable
# here; `source` covers the generating script.
_PLAN_FORMAT = {"3mf": "3mf", "stl": "stl", "step": "step", "source": "source"}

# What each format is called when it reaches the user's disk. One definition,
# because the session route and the signed-link route must not disagree about
# the name of the file they are serving.
_FILENAMES = {
    "3mf": "model.3mf",
    "stl": "model.stl",
    "step": "model.step",
    "source": "source.py",
    "params": "params.json",
    "report": "report.json",
}


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
    # Whose build this is, on a metered deployment. None on the free
    # self-hosted path, which is what makes every credit check a no-op there.
    owner_id: str | None = None

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
    db: Store | None = None,
    allow_unsafe_sandbox: bool = False,
    accounts: Any = None,
    billing_provider: Any = None,
):
    """Build the FastAPI application.

    `accounts` is what turns this from the free self-hosted gateway into the
    hosted product. Left as None -- which is the default, and what the CLI, the
    MCP server and anyone running their own copy get -- there are no auth
    routes, no billing routes, no sessions and no credit check anywhere in the
    request path. Passing an `AccountStore` mounts all of it.

    The switch is a constructor argument rather than a setting read from the
    environment inside a handler, so "is this deployment metered?" is decided
    once, at startup, by the caller who knows.
    """
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
    # `docs/schema.sql` says to start collecting on day one, before anything
    # consumes it, because none of these tables can be backfilled. Defaulting
    # to a file next to the artifacts is what makes that true of a development
    # server as well as a deployment.
    database = db if db is not None else Store(store / "formforge.db")

    app = FastAPI(
        title="FormForge",
        version="0.1.0",
        description=(
            "Natural language to print-ready STL/3MF, via a parametric CAD "
            "kernel rather than a mesh generator."
        ),
    )

    # -- accounts (only when this deployment is metered) ----------------
    metered = accounts is not None
    # Signs download links. Generated per process when unset, which means a
    # restart invalidates outstanding links -- acceptable for something that
    # lives five minutes, and much better than shipping a default that every
    # deployment shares. Set it explicitly to run more than one worker.
    _link_secret = os.environ.get("FORMFORGE_LINK_SECRET") or uuid.uuid4().hex
    if metered:
        from .accounts_router import build_accounts_router
        from .security import COOKIE_NAME

        provider = billing_provider or _default_provider()
        router = build_accounts_router(accounts, provider, store=database)
        app.include_router(router)

        def _session_user(request) -> dict[str, Any] | None:
            return accounts.user_for_token(request.cookies.get(COOKIE_NAME) or "")

        def _authorise_download(model_id: str, fmt: str, request) -> dict[str, Any]:
            """Three separate questions, answered in the order that leaks least.

            1. Is anyone signed in?
            2. Is this model *theirs*? Answered from the persisted `user_id`,
               and a model belonging to somebody else gets a 404 rather than a
               403 -- a 403 confirms the id names something real, which turns
               this endpoint into an oracle for enumerating other people's
               model ids.
            3. Was it paid for, and does their plan include this format?

            Ownership before payment, deliberately: telling a stranger whether
            somebody else's model has been paid for is a disclosure in itself.
            """
            user = _session_user(request)
            if user is None:
                raise HTTPException(status_code=401, detail="Not signed in.")

            record = database.get_model(model_id)
            if record is None or record.get("user_id") != user["id"]:
                raise HTTPException(status_code=404, detail="No such model.")

            # Metadata about their own build is not the paid artifact; the
            # report and the parameters are what a user needs to see *why* a
            # build cost what it did, and paywalling those makes a refund
            # argument unanswerable.
            if fmt in ("report", "params"):
                return user

            if accounts.spend_for_model(model_id) is None:
                raise HTTPException(
                    status_code=402,
                    detail=(
                        "This model has not been paid for. Add credits and it "
                        "will be available."
                    ),
                )
            if not plans.allows_format(user["plan"], _PLAN_FORMAT.get(fmt, fmt)):
                tier = plans.get(user["plan"])
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"The {tier.name} plan includes "
                        f"{', '.join(tier.formats).upper()}. Upgrade to export {fmt.upper()}."
                    ),
                )
            return user

    # -- generation ----------------------------------------------------
    @app.post("/v1/generate", status_code=202)
    async def generate(request: GenerateRequest, background: BackgroundTasks, http: Request):
        owner = None
        if metered:
            user = _session_user(http)
            if user is None:
                raise HTTPException(status_code=401, detail="Not signed in.")
            # Checked before the sandbox runs, not after. A build costs real
            # CPU and a user with no credits cannot pay for it, so refusing up
            # front is both cheaper and a clearer answer than letting them wait
            # for a model they will not be allowed to download.
            #
            # This is a *pre-check*, not a reservation: the credit is taken
            # after validation passes. Two builds started together can both
            # pass here and only one be paid for, which is handled where it
            # happens -- see `_charge_for`.
            accounts.roll_to_current_period(user["id"])
            if accounts.balance(user["id"]) < 1:
                raise HTTPException(
                    status_code=402,
                    detail="You have no credits left. Add credits to keep building.",
                )
            owner = user["id"]
        job = jobs.create()
        job.owner_id = owner
        background.add_task(_run_generation, job, request)
        return {"job_id": job.job_id, "model_id": job.model_id, "status": "queued"}

    def _charge_for(job: Job, result) -> None:
        """Take the credit for a finished build. Only ever called on success.

        The rule, which resolves retries, refreshes and mid-flight crashes
        together: one credit per model, keyed on the model id, at the moment
        validation passes. A second attempt to record the same build is a
        no-op rather than a second charge, so a retried request is free; and a
        crash between here and the file reaching the browser leaves the user
        charged *and* entitled, because entitlement is this ledger row and the
        artifacts are still on disk.

        If the balance went to zero between the pre-check and here -- two
        builds racing for one last credit -- the model is kept, unpaid. It
        becomes downloadable when they have a credit again. Nobody is
        overcharged and nobody gets a paid export they did not pay for.
        """
        if not metered or not job.owner_id or result.status != "ok":
            return
        try:
            accounts.spend(
                job.owner_id,
                model_id=result.model_id,
                note=f"{result.template_id or 'freeform'} build",
            )
        except InsufficientCredits:
            log.warning(
                "model %s finished but could not be paid for; kept unpaid",
                result.model_id,
            )
        except Exception:
            log.exception("could not record the charge for model %s", result.model_id)

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
        except Exception as exc:
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

        # Persisted for every terminal status, not just success: a store that
        # holds only the runs that worked cannot answer a question worth
        # asking. The write is best-effort by design -- see store.Store.
        database.record_generation(result, user_id=job.owner_id, parent_id=job.parent_id)
        # After the record, so a charge always has a model row behind it, and
        # after validation, so only a build that actually succeeded is billable.
        _charge_for(job, result)
        _record_policy(result)

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
            # Recorded with its parent: a modification is a new model with a
            # parent, never an edit in place, and the lineage is only useful
            # if it outlives the process.
            database.record_generation(result, parent_id=child.parent_id)

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

    @app.post("/v1/models/{model_id}/download-link")
    async def download_link(
        model_id: str,
        http: Request,
        format: str = Query("3mf", pattern="^(3mf|stl|step|source|params|report)$"),
    ):
        """A short-lived URL that carries its own authorisation.

        For the cases a session cookie cannot reach -- handing a URL to a
        download manager, or (once artifacts live in a bucket) letting the
        bucket serve it directly. The session route is still the primary path.

        The entitlement checks run *here*, at minting time, so a token can only
        ever exist for a file its holder was already allowed to fetch.
        """
        if not metered:
            raise HTTPException(
                status_code=404, detail="signed links are only issued on a metered deployment"
            )
        user = _authorise_download(model_id, format, http)
        token = sign_download(
            _link_secret, user_id=user["id"], model_id=model_id, fmt=format
        )
        return {
            "url": f"/v1/download/{token}",
            "expires_in": LINK_TTL_SECONDS,
            "format": format,
        }

    @app.get("/v1/download/{token}")
    async def download_signed(token: str):
        """Serve a file to whoever holds a valid token.

        No session required -- that is the point of the token, and also its
        cost: within its few minutes it is a bearer credential for exactly one
        model in exactly one format. Re-checking entitlement here as well as at
        minting time, because a subscription can lapse or a refund can land in
        between, and a token is not a promise about the future.
        """
        if not metered:
            raise HTTPException(status_code=404, detail="not found")
        try:
            grant = verify_download(_link_secret, token)
        except StorageError as exc:
            log.info("rejected a download token: %s", exc)
            raise HTTPException(status_code=403, detail="This link is invalid or has expired.") \
                from None
        record = database.get_model(grant.model_id)
        if record is None or record.get("user_id") != grant.user_id:
            raise HTTPException(status_code=404, detail="No such model.")
        if grant.fmt not in ("report", "params") and accounts.spend_for_model(
            grant.model_id
        ) is None:
            raise HTTPException(status_code=402, detail="This model has not been paid for.")
        return _serve(grant.model_id, grant.fmt)

    @app.get("/v1/models/{model_id}/download")
    async def download(
        model_id: str,
        http: Request,
        format: str = Query("3mf", pattern="^(3mf|stl|step|source|params|report)$"),
    ):
        if metered:
            _authorise_download(model_id, format, http)
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
        return FileResponse(path, filename=_FILENAMES[format])

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
        payload = request.model_dump()
        unknown = [i for i in payload.get("issues") or [] if i not in PRINT_ISSUES]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"unknown issue(s) {', '.join(sorted(unknown))}; "
                    f"expected one of {', '.join(sorted(PRINT_ISSUES))}"
                ),
            )
        if database.get_model(payload["model_id"]) is None:
            # Feedback that points at nothing is feedback that can never be
            # cross-referenced against what the validator measured, which is
            # the only reason this table exists.
            raise HTTPException(
                status_code=404, detail=f"no model {payload['model_id']}"
            )
        feedback_id = database.record_feedback(payload)
        return {"recorded": True, "feedback_id": feedback_id}

    # -- what the collected data is for --------------------------------
    @app.get("/v1/stats")
    async def stats():
        """Totals, template health and the dominant failure classes.

        The three questions this system cannot answer without persistence:
        which templates are quietly failing, which errors actually dominate,
        and whether any of it prints.
        """
        return {
            "totals": database.totals(),
            "templates": database.template_health(),
            "failures": database.failure_classes(),
        }

    @app.get("/v1/stats/prints")
    async def print_stats():
        """Print outcomes against what the validator measured at the time.

        Empty until real prints are reported, and that is the honest state:
        every DFM constant in this system is a conventional maker value until
        this endpoint has rows behind it.
        """
        return {"outcomes": database.print_outcomes()}

    @app.get("/v1/models/{model_id}/events")
    async def model_events(model_id: str):
        """The per-step log for one generation.

        A four-iteration run is inexplicable without this: you can see that it
        took four attempts but not what changed between them.
        """
        events = database.events_for(model_id)
        if not events and database.get_model(model_id) is None:
            raise HTTPException(status_code=404, detail=f"no model {model_id}")
        return {"model_id": model_id, "events": events}

    @app.get("/healthz")
    async def health():
        totals = database.totals()
        return {
            # Telemetry writes are swallowed by design so they can never fail a
            # generation, which means a broken database is silent unless a
            # health check looks for it. This is where it looks.
            "ok": totals["write_failures"] == 0,
            "templates": len(templates),
            "sandbox": sandbox.describe(),
            "model_client": engine.client.available,
            "store": {
                "generations": totals["generations"],
                "prints_reported": totals["prints_reported"],
                "write_failures": totals["write_failures"],
            },
        }

    # -- helpers -------------------------------------------------------
    def _record_policy(result) -> None:
        """Log a refusal or a flag to its own table.

        Kept separate from the generation's event log even though the same
        decision appears there, because the question it answers is about a
        *user* rather than a model: someone working through the IP classifier
        one franchise at a time looks like nothing at all when their attempts
        are filed under the generations they did not produce.
        """
        for event in result.events:
            if event.phase != "policy":
                continue
            decision = (event.payload or {}).get("decision")
            if decision and decision != "allow":
                database.record_policy_event(
                    result.prompt,
                    decision,
                    category=(event.payload or {}).get("category"),
                    matched=(event.payload or {}).get("matched") or [],
                )

    def _require_model(model_id: str) -> Job:
        job = jobs.for_model(model_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no model {model_id}")
        return job

    def _serve(model_id: str, fmt: str):
        """Hand back one artifact, whoever has already been authorised for it."""
        job = _require_result(model_id)
        path = job.result.artifacts.get(fmt)
        if not path or not Path(path).exists():
            raise HTTPException(status_code=404, detail=f"this model has no {fmt} artifact")
        return FileResponse(path, filename=_FILENAMES[fmt])

    def _require_result(model_id: str) -> Job:
        job = _require_model(model_id)
        if job.result is None:
            raise HTTPException(
                status_code=409,
                detail=f"model {model_id} is still {job.status}; poll /status first",
            )
        return job

    # The pieces a test needs to stand in for a real generation. Exposed
    # deliberately rather than reached for through closures: the alternative is
    # tests that reimplement the charging and recording steps, which makes them
    # agree with a copy of the logic rather than with the logic.
    app.state.formforge = {
        "jobs": jobs,
        "db": database,
        "accounts": accounts,
        "charge": _charge_for,
        "store_dir": store,
        "metered": metered,
    }
    return app


app = None
if FASTAPI_AVAILABLE and os.environ.get("FORMFORGE_AUTO_APP") == "1":  # pragma: no cover
    app = create_app()


def _default_provider():
    """The billing provider to use when the caller named none.

    Falls back to the offline one rather than to Stripe. A deployment that
    means to take money says so explicitly; defaulting the other way means a
    misconfiguration silently starts talking to a payment processor, which is
    the wrong direction to be wrong in.
    """
    from ..accounts import OfflineProvider
    from ..accounts.stripe_provider import provider_from_env

    return provider_from_env() or OfflineProvider()
