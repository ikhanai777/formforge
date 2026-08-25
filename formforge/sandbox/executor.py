"""Parent-side driver for the geometry sandbox (spec section 6.1).

Responsibilities, in order: refuse the script statically, spawn an isolated
child, kill it on the wall clock, and turn whatever came back into either a mesh
bundle or a *useful* error. The last part is the one that decides whether the
repair loop converges -- see `formforge.hints`.

Runtimes
--------
`subprocess`  Local development only. rlimits, a stripped environment and the
              import guard -- and *no filesystem isolation whatsoever*. The
              static gate blocks `open()`, but numpy and trimesh are on the
              allowlist and both write files, so a script can put bytes
              anywhere the host user can. There is no network isolation either.
`docker`      Container with no network, read-only rootfs, a tmpfs at /work,
              dropped capabilities, non-root, pids limit.
`gvisor`      As docker, plus `--runtime=runsc`. The production default.

**The isolation lives in the runtime, not in this module.** The static gate and
the import guard raise the cost of the obvious attacks; they do not contain a
determined one, and nothing here should be mistaken for a boundary. The boundary
is the container.

The runtime is chosen by `FORMFORGE_SANDBOX_RUNTIME` so a deployment cannot
accidentally ship the development path: `production_ready()` reports whether the
active runtime provides that boundary, and the API refuses to serve untrusted
traffic when it does not.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .. import security
from ..hints import classify
from .runner import RESULT_BEGIN, RESULT_END

# Runtimes that isolate the guest kernel from the host. Plain docker shares the
# host kernel, so a container escape is a host compromise; gVisor and Firecracker
# interpose a second boundary.
ISOLATED_RUNTIMES = frozenset({"gvisor", "firecracker"})
DEFAULT_RUNTIME = os.environ.get("FORMFORGE_SANDBOX_RUNTIME", "subprocess")
DEFAULT_IMAGE = os.environ.get("FORMFORGE_SANDBOX_IMAGE", "formforge/geometry:latest")


@dataclass(frozen=True)
class Limits:
    """Resource ceilings for one execution."""

    cpu_s: int = 30
    wall_s: int = 45
    mem_mb: int = 2048
    max_triangles: int = 2_000_000
    pids: int = 64
    fsize_mb: int = 512
    tmpfs_mb: int = 256

    def as_job(self) -> dict:
        return {
            "cpu_s": self.cpu_s,
            "mem_mb": self.mem_mb,
            "max_triangles": self.max_triangles,
            "nproc": self.pids,
            "fsize_mb": self.fsize_mb,
        }


@dataclass(frozen=True)
class Tessellation:
    """How finely the B-rep is approximated when it becomes a mesh.

    0.05 mm linear deflection is well under a 0.2 mm layer height, so the
    tessellation is not the limiting factor on print fidelity, and it keeps
    triangle counts in the tens of thousands rather than the millions.
    """

    linear_deflection: float = 0.05
    angular_deflection: float = 0.2

    def as_job(self) -> dict:
        return {
            "linear_deflection": self.linear_deflection,
            "angular_deflection": self.angular_deflection,
        }


@dataclass
class ExecuteRequest:
    source: str
    language: str = "build123d"
    params: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)
    tessellation: Tessellation = field(default_factory=Tessellation)
    # Hand-authored registry templates are human-reviewed, so the magic-number
    # style rule is relaxed for them. It stays on for anything a model wrote.
    enforce_named_constants: bool = True


@dataclass
class ExecuteResult:
    """Either a mesh bundle or an actionable failure. Never both."""

    status: str  # "ok" | "error"
    phase: str = "execute"
    artifacts: dict[str, str] = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    error_class: str | None = None
    message: str | None = None
    traceback: str | None = None
    hint: str | None = None
    violations: list[dict] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    compute_ms: int = 0
    workdir: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict:
        payload: dict = {"status": self.status, "compute_ms": self.compute_ms}
        if self.ok:
            payload["artifacts"] = self.artifacts
            payload["stats"] = self.stats
        else:
            payload.update(
                {
                    "phase": self.phase,
                    "error_class": self.error_class,
                    "message": self.message,
                    "hint": self.hint,
                }
            )
            if self.violations:
                payload["violations"] = self.violations
            if self.traceback:
                payload["traceback"] = self.traceback
        return payload

    def feedback(self) -> str:
        """The failure, formatted for a repair prompt.

        Leads with the hint because that is the part the model can act on; the
        traceback follows as evidence.
        """
        if self.ok:
            return "Execution succeeded."
        lines = [f"EXECUTION FAILED in phase '{self.phase}' ({self.error_class})."]
        if self.hint:
            lines.append(f"\nWHAT WENT WRONG:\n{self.hint}")
        if self.message:
            lines.append(f"\nERROR:\n{self.message}")
        if self.violations:
            lines.append("\nREJECTED CONSTRUCTS:")
            lines += [
                f"  line {v['line']}: {v['message']}"
                for v in self.violations
                if v.get("severity") == "error"
            ]
        if self.traceback:
            lines.append(f"\nTRACEBACK (last frames):\n{_tail(self.traceback, 20)}")
        return "\n".join(lines)


class SandboxUnavailable(RuntimeError):
    """The configured runtime cannot be used on this host."""


class GeometrySandbox:
    """Executes untrusted geometry scripts under the configured runtime."""

    def __init__(
        self,
        runtime: str | None = None,
        image: str | None = None,
        keep_workdir: bool = False,
        base_dir: Path | str | None = None,
    ):
        self.runtime = runtime or DEFAULT_RUNTIME
        self.image = image or DEFAULT_IMAGE
        self.keep_workdir = keep_workdir
        self.base_dir = Path(base_dir) if base_dir else None

    # -- capability reporting ---------------------------------------------
    def production_ready(self) -> bool:
        """Does the active runtime isolate the host kernel?

        The API gateway calls this at startup and refuses untrusted traffic when
        it is false. Shipping the development path to production is the single
        most likely way this system gets someone owned, so it is checked rather
        than documented.
        """
        return self.runtime in ISOLATED_RUNTIMES

    def describe(self) -> dict:
        return {
            "runtime": self.runtime,
            "image": self.image if self.runtime != "subprocess" else None,
            "kernel_isolated": self.production_ready(),
            "warning": (
                None
                if self.production_ready()
                else "no kernel or filesystem isolation: generated code can write "
                "anywhere the host user can, and reach the network. Development "
                "use only."
            ),
        }

    # -- execution ---------------------------------------------------------
    def execute(self, request: ExecuteRequest) -> ExecuteResult:
        started = time.perf_counter()

        gate = security.scan(
            request.source,
            enforce_named_constants=request.enforce_named_constants,
        )
        if not gate.ok:
            first = gate.errors[0]
            return ExecuteResult(
                status="error",
                phase="static_gate",
                error_class="StaticGateRejection",
                message=first.message,
                hint=_gate_hint(gate),
                violations=[v.as_dict() for v in gate.violations],
                compute_ms=int((time.perf_counter() - started) * 1000),
            )

        workdir = self._make_workdir()
        job = {
            "language": request.language,
            "source": request.source,
            "params": request.params,
            "metadata": request.metadata,
            "limits": request.limits.as_job(),
            "tessellation": request.tessellation.as_job(),
            "allowed_imports": sorted(security.ALLOWED_IMPORTS),
            "workdir": str(self._guest_workdir(workdir)),
        }

        try:
            proc = self._spawn(job, workdir, request.limits)
        except SandboxUnavailable as exc:
            self._cleanup(workdir)
            return ExecuteResult(
                status="error",
                phase="sandbox",
                error_class="SandboxUnavailable",
                message=str(exc),
                hint="The geometry sandbox is not available on this host. This is "
                "an infrastructure fault, not a problem with the model.",
                compute_ms=int((time.perf_counter() - started) * 1000),
            )

        compute_ms = int((time.perf_counter() - started) * 1000)
        result = self._interpret(proc, workdir, request, compute_ms)
        if not (result.ok or self.keep_workdir):
            self._cleanup(workdir)
        return result

    # -- internals ---------------------------------------------------------
    def _make_workdir(self) -> Path:
        base = self.base_dir or Path(tempfile.gettempdir()) / "formforge"
        base.mkdir(parents=True, exist_ok=True)
        workdir = base / f"job-{uuid.uuid4().hex[:12]}"
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir

    def _guest_workdir(self, host_workdir: Path) -> Path:
        """Where the workdir appears from inside the sandbox."""
        return Path("/work") if self.runtime != "subprocess" else host_workdir

    def _cleanup(self, workdir: Path) -> None:
        shutil.rmtree(workdir, ignore_errors=True)

    def _spawn(
        self, job: dict, workdir: Path, limits: Limits
    ) -> subprocess.CompletedProcess[str]:
        if self.runtime == "subprocess":
            cmd = self._subprocess_cmd()
        elif self.runtime in {"docker", "gvisor", "firecracker"}:
            cmd = self._container_cmd(workdir, limits)
        else:
            raise SandboxUnavailable(f"unknown sandbox runtime {self.runtime!r}")

        try:
            return subprocess.run(  # noqa: S603 -- argv is built here, never shell
                cmd,
                input=json.dumps(job),
                capture_output=True,
                text=True,
                timeout=limits.wall_s,
                check=False,
                cwd=str(workdir),
            )
        except subprocess.TimeoutExpired as exc:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=124,
                stdout=_decode(exc.stdout),
                stderr="wall-clock timeout",
            )
        except FileNotFoundError as exc:
            raise SandboxUnavailable(
                f"sandbox runtime {self.runtime!r} is not installed: {exc}"
            ) from exc

    def _subprocess_cmd(self) -> list[str]:
        # Run the runner by path rather than with -m: `-I` deliberately ignores
        # PYTHONPATH, so a module lookup would not find the package, and the
        # runner is written to have no relative imports precisely so it can be
        # invoked this way (or copied into an image on its own).
        runner = Path(__file__).with_name("runner.py")
        return [sys.executable, "-I", str(runner)]

    def _container_cmd(self, workdir: Path, limits: Limits) -> list[str]:
        """A container with every capability the job does not need removed.

        No network is the important one: it makes exfiltration impossible even
        if a prompt injection succeeds in getting arbitrary code executed
        (spec section 10.1).
        """
        cmd = [
            "docker",
            "run",
            "--rm",
            "--interactive",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=65534:65534",
            f"--pids-limit={limits.pids}",
            f"--memory={limits.mem_mb}m",
            f"--memory-swap={limits.mem_mb}m",
            f"--cpus={max(1, limits.cpu_s // 30)}",
            f"--tmpfs=/work:rw,size={limits.tmpfs_mb}m,mode=1777",
            f"--tmpfs=/tmp:rw,size=64m,mode=1777",
            "--env-file=/dev/null",
            "--volume",
            f"{workdir}:/out:rw",
        ]
        if self.runtime == "gvisor":
            cmd += ["--runtime=runsc"]
        seccomp = os.environ.get("FORMFORGE_SECCOMP_PROFILE")
        if seccomp:
            cmd += [f"--security-opt=seccomp={seccomp}"]
        cmd += [self.image, "python", "-I", "-S", "-m", "formforge.sandbox.runner"]
        return cmd

    def _interpret(
        self,
        proc: subprocess.CompletedProcess[str],
        workdir: Path,
        request: ExecuteRequest,
        compute_ms: int,
    ) -> ExecuteResult:
        payload = _extract_result(proc.stdout or "")

        if payload is None:
            return self._interpret_crash(proc, compute_ms)

        if payload.get("status") == "ok":
            artifacts = _rehome(payload.get("artifacts") or {}, workdir, self.runtime)
            stats = payload.get("stats") or {}
            stats["compute_ms"] = compute_ms
            return ExecuteResult(
                status="ok",
                artifacts=artifacts,
                stats=stats,
                stdout=payload.get("stdout", ""),
                stderr=payload.get("stderr", ""),
                compute_ms=compute_ms,
                workdir=str(workdir),
            )

        message = _sanitize(payload.get("message", ""))
        tb = _sanitize(payload.get("traceback", ""))
        error_class, hint = classify(f"{payload.get('exception', '')}: {message}\n{tb}")
        return ExecuteResult(
            status="error",
            phase=payload.get("phase", "execute"),
            error_class=error_class,
            message=message,
            traceback=tb,
            hint=hint,
            compute_ms=compute_ms,
            workdir=str(workdir),
        )

    def _interpret_crash(
        self, proc: subprocess.CompletedProcess[str], compute_ms: int
    ) -> ExecuteResult:
        """No result payload: the child died before it could report."""
        if proc.returncode == 124:
            return ExecuteResult(
                status="error",
                phase="execute",
                error_class="Timeout",
                message=f"execution exceeded the wall-clock limit",
                hint="The script did not finish in time. This is almost always a "
                "pattern with too many instances, a tessellation deflection set "
                "far too fine, or a boolean against a very high-facet solid. "
                "Reduce the instance count and coarsen the tessellation.",
                compute_ms=compute_ms,
            )
        stderr = _sanitize(proc.stderr or "")
        # SIGXCPU (24) is what the CPU rlimit raises, and SIGKILL (9) is what
        # lands when the hard limit follows. Both mean the script ran too long,
        # and saying so is far more useful than the generic crash path.
        if proc.returncode in (-24, -152, 152) or "SIGXCPU" in stderr:
            return ExecuteResult(
                status="error",
                phase="execute",
                error_class="Timeout",
                message="the script exceeded its CPU time limit",
                hint="The script did not finish in time. This is almost always a "
                "pattern with too many instances, a tessellation deflection set "
                "far too fine, or a boolean against a very high-facet solid. "
                "Reduce the instance count and coarsen the tessellation.",
                compute_ms=compute_ms,
            )
        if proc.returncode in (-9, 137) or "MemoryError" in stderr:
            return ExecuteResult(
                status="error",
                phase="execute",
                error_class="OutOfMemory",
                message="the sandbox was killed for exceeding its memory limit",
                hint="The script exhausted memory. Reduce the number of patterned "
                "instances or coarsen linear_deflection.",
                compute_ms=compute_ms,
            )
        error_class, hint = classify(stderr)
        return ExecuteResult(
            status="error",
            phase="sandbox",
            error_class=error_class or "SandboxCrash",
            message=stderr[-2000:] or f"sandbox exited with code {proc.returncode}",
            hint=hint,
            compute_ms=compute_ms,
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_RESULT_RE = re.compile(
    re.escape(RESULT_BEGIN) + r"\s*(.*?)\s*" + re.escape(RESULT_END), re.DOTALL
)

# Host paths in a traceback are both an information leak and noise that wastes
# repair-prompt tokens.
_PATH_RE = re.compile(r'(?:/[\w.\-]+)+/([\w.\-]+\.py)')
_TMP_RE = re.compile(r"/(?:tmp|var|home|root|Users)/[^\s\"']*")


def _extract_result(stdout: str) -> dict | None:
    """Decode the result frame the runner wrote.

    The payload is base64 so that nothing a script prints -- which is echoed
    back inside the payload's own `stdout` field -- can contain a sentinel and
    truncate the frame.
    """
    match = _RESULT_RE.search(stdout)
    if not match:
        return None
    body = match.group(1).strip()
    try:
        return json.loads(base64.b64decode(body, validate=True).decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _sanitize(text: str) -> str:
    """Strip host paths from anything that reaches a prompt or a user."""
    if not text:
        return ""
    text = _TMP_RE.sub("<path>", text)
    text = _PATH_RE.sub(r"\1", text)
    return text


def _tail(text: str, lines: int) -> str:
    parts = text.strip().splitlines()
    return "\n".join(parts[-lines:])


def _decode(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


def _rehome(artifacts: dict, workdir: Path, runtime: str) -> dict:
    """Rewrite guest paths to host paths after a container run."""
    if runtime == "subprocess":
        return artifacts
    out: dict[str, str] = {}
    for key, value in artifacts.items():
        if key.endswith("_error"):
            out[key] = value
            continue
        out[key] = str(workdir / Path(value).name)
    return out


def _gate_hint(gate: security.ScanResult) -> str:
    """Turn static-gate violations into one instruction the model can follow."""
    rules = {v.rule for v in gate.errors}
    parts: list[str] = []
    if "import" in rules:
        parts.append(
            "Remove the disallowed imports. A geometry script needs only "
            "build123d (and optionally math/numpy); it has no filesystem, "
            "network or process access and does not need any module that "
            "provides them."
        )
    if "banned-name" in rules:
        parts.append(
            "Remove the use of dynamic-execution or filesystem builtins. "
            "Build the geometry directly."
        )
    if "dunder-attribute" in rules:
        parts.append("Do not access dunder attributes.")
    if "magic-number" in rules:
        offenders = sorted(
            {
                m.group(1)
                for v in gate.errors
                if v.rule == "magic-number"
                for m in [re.search(r"literal ([\d.]+)", v.message)]
                if m
            }
        )
        parts.append(
            "Every dimension must be a named module-level constant, not a "
            "literal inside a geometry call. Hoist these values to constants "
            f"at the top of the script: {', '.join(offenders[:12])}."
        )
    if "unbounded-while" in rules:
        parts.append("Replace the unbounded while loop with a for loop over a range.")
    if "syntax" in rules:
        parts.append("The script does not parse. Fix the syntax error.")
    return " ".join(parts) or "The script was rejected by the static gate."


# A module-level default so callers that do not need configuration can just
# import and go.
default_sandbox = GeometrySandbox()


def execute(request: ExecuteRequest) -> ExecuteResult:
    """Execute a request on the default sandbox."""
    return default_sandbox.execute(request)
