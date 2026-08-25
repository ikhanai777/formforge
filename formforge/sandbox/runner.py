"""In-sandbox entrypoint: execute one geometry script and export the result.

This module runs inside the isolated child process. It must not import anything
from the rest of FormForge -- it is copied into the sandbox and run with a bare
interpreter, so its only dependencies are the standard library and the geometry
stack itself.

Protocol: a JSON job object arrives on stdin, a JSON result object is written to
stdout between sentinel lines. Everything the script itself prints is captured
separately, so a `print()` in generated code cannot forge a result.

The hardening applied here (rlimits, import allowlist, stripped builtins) is the
inner layer only. The outer layer -- no network, read-only rootfs, gVisor or a
microVM, ephemeral container -- is applied by the executor and is the control
that actually matters (spec section 10.1).
"""

from __future__ import annotations

import builtins
import contextlib
import io
import json
import os
import resource
import sys
import traceback
from pathlib import Path

RESULT_BEGIN = "<<<FORMFORGE_RESULT_BEGIN>>>"
RESULT_END = "<<<FORMFORGE_RESULT_END>>>"

# Names the script may assign its finished solid to, in order of preference.
RESULT_NAMES = ("result", "part", "model", "RESULT", "PART", "MODEL")

# Modules the import hook permits. Kept in sync with formforge.security by the
# executor, which passes the list in the job so the two cannot drift.
DEFAULT_ALLOWED_IMPORTS = (
    "build123d",
    "cadquery",
    "math",
    "cmath",
    "numpy",
    "trimesh",
    "typing",
    "dataclasses",
    "enum",
    "itertools",
    "functools",
    "statistics",
    "random",
    "copy",
)

# Builtins removed from the script's namespace. The static gate rejects these
# too; doing it twice costs nothing and closes the gap where a script obtains a
# reference indirectly.
#
# `__import__` is deliberately absent from this list: removing it breaks the
# `import` statement itself. It is replaced by a guarded version instead, which
# is the stronger control anyway -- it sees dynamically constructed module names
# that a static scan cannot.
STRIPPED_BUILTINS = (
    "eval",
    "exec",
    "compile",
    "open",
    "input",
    "globals",
    "locals",
    "vars",
    "breakpoint",
    "help",
    "exit",
    "quit",
    "memoryview",
)

# A fixed timestamp so a STEP export is byte-reproducible: the same script and
# parameters must produce the same file, or artifact caching and diffing break.
FIXED_TIMESTAMP = "2026-01-01T00:00:00"


def apply_limits(limits: dict) -> None:
    """Apply POSIX resource limits to this process before running anything."""
    cpu_s = int(limits.get("cpu_s", 30))
    mem_mb = int(limits.get("mem_mb", 2048))
    nproc = int(limits.get("nproc", 64))
    fsize_mb = int(limits.get("fsize_mb", 512))

    def _set(what: int, soft: int, hard: int | None = None) -> None:
        with contextlib.suppress(ValueError, OSError, resource.error):
            resource.setrlimit(what, (soft, hard if hard is not None else soft))

    # A one-second gap between soft and hard: the soft limit raises SIGXCPU,
    # which we can turn into a legible timeout error, before SIGKILL lands.
    _set(resource.RLIMIT_CPU, cpu_s, cpu_s + 1)
    _set(resource.RLIMIT_AS, mem_mb * 1024 * 1024)
    _set(resource.RLIMIT_FSIZE, fsize_mb * 1024 * 1024)
    _set(resource.RLIMIT_NPROC, nproc)
    _set(resource.RLIMIT_CORE, 0)


def strip_environment() -> None:
    """Empty the environment so no credential can reach generated code.

    The orchestrator passes only source and parameters to the geometry tier
    (spec section 10.1); this is the belt to that braces.
    """
    keep = {"PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH", "TMPDIR", "PYTHONHASHSEED"}
    for key in list(os.environ):
        if key not in keep:
            del os.environ[key]


def restricted_builtins(allowed: tuple[str, ...]) -> dict:
    """Builtins for the script's namespace: dangerous names out, imports guarded.

    Note this restricts only the *generated script's* globals. build123d and
    numpy keep their own module namespaces with the real builtins, so guarding
    here costs the geometry stack nothing.
    """
    safe = {k: v for k, v in vars(builtins).items() if k not in STRIPPED_BUILTINS}
    safe["__import__"] = _guarded_import(frozenset(allowed))
    return safe


def _guarded_import(allowed: frozenset[str]):
    """An `__import__` that refuses anything outside the allowlist.

    Catches the dynamic case the static gate cannot see: a name assembled at
    runtime, or an import buried inside a function body that only executes on
    some parameter values.
    """
    real_import = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
        root = name.split(".")[0]
        if root not in allowed:
            raise ImportError(
                f"import of {name!r} is blocked by the FormForge sandbox; "
                f"permitted modules: {', '.join(sorted(allowed))}"
            )
        return real_import(name, globals, locals, fromlist, level)

    return guarded


# ---------------------------------------------------------------------------
# Result extraction
# ---------------------------------------------------------------------------


def _shape_like(obj: object) -> bool:
    """Is this a build123d/CadQuery shape we can export?"""
    for attr in ("wrapped", "volume"):
        if not hasattr(obj, attr):
            return False
    return True


def find_result(namespace: dict, shown: list) -> object:
    """Locate the finished solid in the executed namespace.

    Preference order: an explicit `result`/`part`/`model` binding, then anything
    passed to `show_object()`, then the largest shape left in the namespace.
    The last case is a convenience for models that forget the convention; it is
    reported in the result so the caller knows it happened.
    """
    for name in RESULT_NAMES:
        obj = namespace.get(name)
        if obj is not None and _shape_like(obj):
            return _unwrap(obj)
    if shown:
        return _unwrap(shown[-1])
    candidates = [
        obj
        for key, obj in namespace.items()
        if not key.startswith("_") and _shape_like(obj)
    ]
    if not candidates:
        raise ValueError(
            "the script produced no geometry: assign the finished solid to a "
            "module-level variable named `result`"
        )
    return _unwrap(max(candidates, key=_safe_volume))


def _unwrap(obj: object):
    """Reduce a builder or CadQuery workplane to the underlying shape."""
    # build123d builder object (BuildPart etc.) exposes `.part`/`.sketch`/`.line`
    for attr in ("part", "sketch", "line"):
        inner = getattr(obj, attr, None)
        if inner is not None and _shape_like(inner):
            return inner
    # CadQuery Workplane
    val = getattr(obj, "val", None)
    if callable(val):
        with contextlib.suppress(Exception):
            inner = val()
            if _shape_like(inner):
                return inner
    return obj


def _safe_volume(obj: object) -> float:
    try:
        return float(obj.volume)  # type: ignore[attr-defined]
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_all(shape, workdir: Path, tessellation: dict, metadata: dict) -> dict:
    """Write STL, STEP and 3MF, returning per-format paths and mesh stats."""
    from build123d import Mesher, Unit, export_step, export_stl  # noqa: PLC0415

    linear = float(tessellation.get("linear_deflection", 0.05))
    angular = float(tessellation.get("angular_deflection", 0.2))

    artifacts: dict[str, str] = {}

    stl_path = workdir / "model.stl"
    export_stl(shape, stl_path, tolerance=linear, angular_tolerance=angular)
    artifacts["stl"] = str(stl_path)

    # STEP is the parametric path's differentiator (spec section 3.2): it is
    # what lets a user open the result in real CAD and keep editing.
    step_path = workdir / "model.step"
    try:
        export_step(shape, step_path, unit=Unit.MM, timestamp=FIXED_TIMESTAMP)
        artifacts["step"] = str(step_path)
    except Exception as exc:  # STEP is desirable, not required
        artifacts["step_error"] = f"{type(exc).__name__}: {exc}"

    # 3MF is the primary deliverable because it declares its units. Every
    # "my print came out 25.4x too big" ticket is an STL unit ambiguity.
    mmf_path = workdir / "model.3mf"
    try:
        mesher = Mesher(unit=Unit.MM)
        mesher.add_shape(shape, linear_deflection=linear, angular_deflection=angular)
        for key, value in metadata.items():
            with contextlib.suppress(Exception):
                mesher.add_meta_data(
                    name_space="formforge",
                    name=str(key),
                    value=str(value),
                    metadata_type="str",
                    must_preserve=False,
                )
        mesher.write(mmf_path)
        artifacts["3mf"] = str(mmf_path)
    except Exception as exc:
        artifacts["3mf_error"] = f"{type(exc).__name__}: {exc}"

    return artifacts


def kernel_stats(shape) -> dict:
    """Exact B-rep measurements, taken from the kernel rather than the mesh.

    These are the numbers the dimensional-fidelity check is scored against:
    a tessellation is an approximation, the B-rep is the truth.
    """
    bbox = shape.bounding_box()
    stats: dict[str, object] = {
        "volume_mm3": round(float(shape.volume), 6),
        "bbox_mm": [
            round(float(bbox.size.X), 6),
            round(float(bbox.size.Y), 6),
            round(float(bbox.size.Z), 6),
        ],
        "bbox_min_mm": [
            round(float(bbox.min.X), 6),
            round(float(bbox.min.Y), 6),
            round(float(bbox.min.Z), 6),
        ],
    }
    with contextlib.suppress(Exception):
        stats["solids"] = len(shape.solids())
    with contextlib.suppress(Exception):
        stats["shells"] = len(shape.shells())
    with contextlib.suppress(Exception):
        stats["faces"] = len(shape.faces())
    with contextlib.suppress(Exception):
        stats["is_valid"] = bool(shape.is_valid())
    return stats


def brep_features(shape) -> dict:
    """Exact feature measurements taken from the B-rep, not the mesh.

    Hole diameters are the motivating case. Detecting a circle by fitting one to
    a tessellated boundary is fiddly and approximate; asking the kernel for the
    radius of a cylindrical face is exact and takes microseconds. The same goes
    for plate contact area, where a mesh-based measurement has to pick an
    epsilon and this does not.

    Only available on the parametric path -- the OpenSCAD path has no B-rep and
    the validator falls back to mesh heuristics there.
    """
    features: dict[str, list] = {"cylinders": [], "planes": []}
    try:
        bbox = shape.bounding_box()
        z_min = float(bbox.min.Z)
        faces = shape.faces()
    except Exception:
        return features

    for face in faces:
        try:
            geom = str(face.geom_type).rsplit(".", 1)[-1]
            area = float(face.area)
        except Exception:
            continue

        if geom == "CYLINDER":
            with contextlib.suppress(Exception):
                radius = float(face.radius)
                axis = face.axis_of_rotation
                features["cylinders"].append(
                    {
                        "radius_mm": round(radius, 6),
                        "diameter_mm": round(radius * 2, 6),
                        "area_mm2": round(area, 6),
                        "axis": [
                            round(float(axis.direction.X), 6),
                            round(float(axis.direction.Y), 6),
                            round(float(axis.direction.Z), 6),
                        ],
                        # Height of the cylindrical band: area / circumference.
                        "length_mm": round(area / (2 * 3.141592653589793 * radius), 6)
                        if radius > 1e-9
                        else 0.0,
                        "internal": _is_internal_cylinder(face),
                    }
                )
        elif geom == "PLANE":
            with contextlib.suppress(Exception):
                center = face.center()
                normal = face.normal_at(center)
                features["planes"].append(
                    {
                        "area_mm2": round(area, 6),
                        "normal": [
                            round(float(normal.X), 6),
                            round(float(normal.Y), 6),
                            round(float(normal.Z), 6),
                        ],
                        "center_z_mm": round(float(center.Z), 6),
                        "on_plate": bool(abs(float(center.Z) - z_min) < 1e-6
                                         and float(normal.Z) < -0.9),
                    }
                )
    return features


def _is_internal_cylinder(face) -> bool:
    """Does this cylindrical face bound a hole (normal points at the axis)?

    A hole's outward-from-solid normal points inward toward the axis; a boss's
    points away from it. Distinguishing the two is what separates "your hole is
    too small to print" from "your pin is too thin to survive".
    """
    try:
        center = face.center()
        normal = face.normal_at(center)
        axis = face.axis_of_rotation
        origin = axis.position
        direction = axis.direction
        # Vector from the axis to the surface point, with the axial component
        # projected out.
        dx = float(center.X) - float(origin.X)
        dy = float(center.Y) - float(origin.Y)
        dz = float(center.Z) - float(origin.Z)
        ax, ay, az = float(direction.X), float(direction.Y), float(direction.Z)
        axial = dx * ax + dy * ay + dz * az
        rx, ry, rz = dx - axial * ax, dy - axial * ay, dz - axial * az
        dot = rx * float(normal.X) + ry * float(normal.Y) + rz * float(normal.Z)
        return dot < 0
    except Exception:
        return False


def triangle_count(stl_path: Path) -> int:
    """Read the triangle count from a binary STL header without parsing it."""
    with contextlib.suppress(Exception):
        data = stl_path.read_bytes()[:84]
        if len(data) >= 84:
            return int.from_bytes(data[80:84], "little")
    return 0


# ---------------------------------------------------------------------------
# Job execution
# ---------------------------------------------------------------------------


def run_python(job: dict, workdir: Path) -> dict:
    """Execute a build123d/CadQuery script and export what it produced."""
    source = job["source"]
    allowed = tuple(job.get("allowed_imports") or DEFAULT_ALLOWED_IMPORTS)

    # The guard goes on the script's own `__import__`, not on sys.meta_path: a
    # meta-path hook would also intercept build123d's and numpy's internal lazy
    # imports of OCP, scipy and friends, and break the geometry stack itself.
    shown: list = []

    def show_object(obj, *_args, **_kwargs):
        """CQ-editor compatibility: many training examples end with this."""
        shown.append(obj)
        return obj

    namespace: dict = {
        "__name__": "__formforge__",
        "__builtins__": restricted_builtins(allowed),
        "show_object": show_object,
        "params": job.get("params") or {},
    }

    stdout = io.StringIO()
    stderr = io.StringIO()
    code = compile(source, "<generated>", "exec")
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exec(code, namespace)  # noqa: S102 -- this is the sandbox's entire purpose

    shape = find_result(namespace, shown)
    stats = kernel_stats(shape)
    if stats["volume_mm3"] <= 0:
        raise ValueError(
            "the resulting shape has no volume -- a subtraction probably removed "
            "everything, or the script returned a sketch rather than a solid"
        )

    stats["brep_features"] = brep_features(shape)
    artifacts = export_all(shape, workdir, job.get("tessellation") or {}, job.get("metadata") or {})
    if "stl" in artifacts:
        stats["triangles"] = triangle_count(Path(artifacts["stl"]))

    max_triangles = int((job.get("limits") or {}).get("max_triangles", 2_000_000))
    if stats.get("triangles", 0) > max_triangles:
        raise ValueError(
            f"mesh has {stats['triangles']} triangles, over the {max_triangles} cap; "
            f"coarsen linear_deflection or simplify the geometry"
        )

    return {
        "artifacts": artifacts,
        "stats": stats,
        "stdout": stdout.getvalue()[-4000:],
        "stderr": stderr.getvalue()[-4000:],
    }


def run_openscad(job: dict, workdir: Path) -> dict:
    """Render an OpenSCAD script via the CLI.

    Kept as the fallback target (spec section 3.1): it sandboxes trivially
    because it is a DSL rather than a general-purpose language, and it is far
    better than build123d at lattices and pattern work. It cannot produce STEP
    or a true fillet, so the bundle from this path is mesh-only.
    """
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    binary = shutil.which("openscad") or shutil.which("openscad-nightly")
    if not binary:
        raise RuntimeError(
            "the openscad binary is not available in this sandbox image; "
            "regenerate this model with language='build123d'"
        )

    scad_path = workdir / "model.scad"
    scad_path.write_text(job["source"], encoding="utf-8")
    stl_path = workdir / "model.stl"

    cmd = [binary, "--export-format", "binstl", "-o", str(stl_path)]
    for key, value in (job.get("params") or {}).items():
        cmd += ["-D", f"{key}={json.dumps(value)}"]
    cmd.append(str(scad_path))

    proc = subprocess.run(  # noqa: S603 -- fixed binary, no shell
        cmd,
        capture_output=True,
        text=True,
        timeout=int((job.get("limits") or {}).get("cpu_s", 30)),
        check=False,
    )
    if proc.returncode != 0 or not stl_path.exists():
        raise RuntimeError(f"openscad failed: {proc.stderr.strip()[:2000]}")

    stats = _mesh_stats(stl_path)
    stats["triangles"] = triangle_count(stl_path)
    return {
        "artifacts": {"stl": str(stl_path), "scad": str(scad_path)},
        "stats": stats,
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-4000:],
    }


def _mesh_stats(stl_path: Path) -> dict:
    """Mesh-derived stats, for the OpenSCAD path where there is no B-rep."""
    import trimesh  # noqa: PLC0415

    mesh = trimesh.load(stl_path, force="mesh")
    extents = [round(float(v), 6) for v in mesh.extents]
    bounds_min = [round(float(v), 6) for v in mesh.bounds[0]]
    return {
        "volume_mm3": round(float(abs(mesh.volume)), 6),
        "bbox_mm": extents,
        "bbox_min_mm": bounds_min,
        "solids": int(mesh.body_count),
        "shells": int(mesh.body_count),
        "faces": int(len(mesh.faces)),
        "is_valid": bool(mesh.is_watertight),
    }


def main() -> int:
    raw = sys.stdin.read()
    try:
        job = json.loads(raw)
    except json.JSONDecodeError as exc:
        _emit({"status": "error", "phase": "job", "message": f"bad job payload: {exc}"})
        return 2

    workdir = Path(job.get("workdir") or ".")
    workdir.mkdir(parents=True, exist_ok=True)

    strip_environment()
    apply_limits(job.get("limits") or {})

    language = job.get("language", "build123d")
    phase = "execute"
    try:
        if language == "openscad":
            payload = run_openscad(job, workdir)
        else:
            payload = run_python(job, workdir)
        payload["status"] = "ok"
        _emit(payload)
        return 0
    except Exception as exc:
        tb = traceback.format_exc()
        _emit(
            {
                "status": "error",
                "phase": phase,
                "exception": type(exc).__name__,
                "message": str(exc)[:4000],
                "traceback": tb[-8000:],
            }
        )
        return 1


def _emit(payload: dict) -> None:
    """Write the result between sentinels so script output cannot forge it."""
    sys.stdout.flush()
    sys.__stdout__.write(f"\n{RESULT_BEGIN}\n")
    sys.__stdout__.write(json.dumps(payload))
    sys.__stdout__.write(f"\n{RESULT_END}\n")
    sys.__stdout__.flush()


if __name__ == "__main__":
    raise SystemExit(main())
