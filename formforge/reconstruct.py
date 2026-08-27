"""Image-to-3D through a locally hosted reconstruction model.

This is the other half of the image path, and it is deliberately not the
`emboss` half. Tracing a silhouette infers nothing and produces an exact
contour; reconstruction infers the whole far side of the object and produces a
guess. Both are legitimate, but only one of them can hand you a chair.

What this module does *not* do is run the model. Weights and a GPU belong on
the machine that has them, so the contract here is an HTTP one and the model
is whatever the user has installed behind it. What FormForge adds is the part
that decides whether the result can be printed:

  - **Scale.** A generative mesh is unitless. Nothing in it says millimetres,
    and a mesh that "looks like a chair" at 1.0 units is not a model of
    anything until somebody says how big the real thing is. So a real
    dimension is required rather than guessed, which is the same standard the
    rest of the system holds itself to.
  - **Floaters.** Learned meshes routinely carry stray shards that no slicer
    can interpret. The largest component is the object; the rest is noise, and
    dropping it is reported rather than silent.
  - **Repair.** `repair.py` already draws the line that matters: lossy rungs
    are refused on the parametric path because they make the STL disagree with
    the STEP file. Here there is no STEP file to disagree with -- the mesh was
    never derived from CAD -- so the lossy rungs are allowed, and the fact
    that they ran is recorded.
  - **Validation.** The same three tiers as everything else. A reconstruction
    that cannot be printed should fail here rather than on the plate.

The honest limitation, stated once: the output is a mesh. There is no B-rep to
export, so there is no STEP file and no editable `source.py`. That is the
trade the README's comparison table describes, and choosing this path is
choosing that side of it.
"""

from __future__ import annotations

import json
import mimetypes
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import trimesh

__all__ = [
    "ReconstructError",
    "ReconstructOptions",
    "ReconstructResult",
    "clean",
    "fetch_mesh",
    "is_local",
    "reconstruct",
]

LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def is_local(url: str) -> bool:
    """Whether a backend URL stays on this machine.

    Not a permission check -- a box on the LAN is a perfectly reasonable place
    to keep a GPU. It is so the CLI can say out loud when the images are about
    to leave the machine, which should never be a surprise.
    """
    return (urllib.parse.urlparse(url).hostname or "") in LOOPBACK
MESH_SUFFIX = {
    "model/gltf-binary": ".glb",
    "model/gltf+json": ".gltf",
    "model/obj": ".obj",
    "text/plain": ".obj",
    "application/octet-stream": ".glb",
    "model/stl": ".stl",
    "model/ply": ".ply",
}


class ReconstructError(RuntimeError):
    """The backend could not be reached, or did not return a usable mesh."""


@dataclass
class ReconstructOptions:
    url: str = "http://127.0.0.1:8300"
    # The one dimension that makes the result a model of something. Which axis
    # it applies to is the caller's, because a chair is known by its height and
    # a table by its width.
    size_mm: float = 0.0
    size_axis: str = "z"
    timeout_s: float = 900.0
    max_triangles: int = 200_000
    allow_lossy_repair: bool = True
    keep_floaters: bool = False
    # Fraction of the largest component below which a piece is noise rather
    # than part of the object.
    floater_frac: float = 0.02
    output_format: str = "glb"


@dataclass
class ReconstructResult:
    mesh: trimesh.Trimesh | None = None
    notes: list[str] = field(default_factory=list)
    repair: dict = field(default_factory=dict)
    dropped_components: int = 0
    raw_triangles: int = 0
    backend: str = ""

    def summary(self) -> str:
        if self.mesh is None:
            return "no mesh"
        return (
            f"{len(self.mesh.faces)} triangles, "
            f"{'watertight' if self.mesh.is_watertight else 'NOT watertight'}"
        )


# --------------------------------------------------------------------- http


def _multipart(images: list[Path], fields: dict[str, str]) -> tuple[bytes, str]:
    """Encode a multipart body without adding an HTTP dependency."""
    boundary = f"----formforge{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n"
            f"{value}\r\n".encode()
        )
    for path in images:
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="{path.name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n".encode()
        )
        parts.append(path.read_bytes())
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def fetch_mesh(images: list[Path], opts: ReconstructOptions) -> tuple[bytes, str]:
    """POST the views to the backend and return the mesh bytes it produces.

    Synchronous by design. A 202 with a job id is honoured for backends that
    queue, because a reconstruction takes minutes on most hardware and pinning
    the contract to a single request would rule those out.
    """
    body, content_type = _multipart(images, {"format": opts.output_format})
    request = urllib.request.Request(
        urllib.parse.urljoin(opts.url + "/", "reconstruct"),
        data=body,
        headers={"Content-Type": content_type, "Accept": "*/*"},
        method="POST",
    )
    if request.type not in {"http", "https"}:
        raise ReconstructError(f"{opts.url!r} is not an http(s) URL")

    try:
        with urllib.request.urlopen(request, timeout=opts.timeout_s) as response:
            payload = response.read()
            ctype = response.headers.get("Content-Type", "").split(";")[0].strip()
            status = response.status
    except urllib.error.HTTPError as exc:
        raise ReconstructError(
            f"the backend returned HTTP {exc.code}: {exc.read()[:400].decode(errors='replace')}"
        ) from exc
    except OSError as exc:
        raise ReconstructError(
            f"could not reach a reconstruction backend at {opts.url}: {exc}. "
            f"Start one on that address, or point --url at the machine running it."
        ) from exc

    if status == 202 or ctype == "application/json":
        try:
            job = json.loads(payload)
        except ValueError as exc:
            raise ReconstructError("the backend returned JSON that does not parse") from exc
        raise ReconstructError(
            "this backend queues work and returned job "
            f"{job.get('job_id', '?')!r}; polling backends are not supported yet, "
            "so wrap it in a handler that blocks until the mesh is ready"
        )

    if not payload:
        raise ReconstructError("the backend returned an empty body")
    return payload, MESH_SUFFIX.get(ctype, f".{opts.output_format}")


# -------------------------------------------------------------------- clean


def _bulk(piece: trimesh.Trimesh) -> float:
    """How much space a piece actually occupies, however torn it is."""
    try:
        volume = float(abs(piece.convex_hull.volume))
        if volume > 0:
            return volume
    except Exception:
        pass
    extents = piece.bounds[1] - piece.bounds[0]
    return float(np.prod(np.maximum(extents, 1e-9)))


def clean(mesh: trimesh.Trimesh, opts: ReconstructOptions) -> ReconstructResult:
    """Turn whatever the model produced into something a slicer can read."""
    from .repair import repair

    out = ReconstructResult(raw_triangles=len(mesh.faces))
    working = mesh.copy()

    if not opts.keep_floaters:
        pieces = working.split(only_watertight=False)
        if len(pieces) > 1:
            # Rank by occupied volume, not by face count. Face count measures
            # how finely a piece was tessellated, not how big it is, and a
            # coarse body next to a dense speck ranks exactly backwards. The
            # convex hull is used because a floater is rarely watertight and
            # `.volume` is meaningless when it is not.
            sizes = [_bulk(p) for p in pieces]
            biggest = max(sizes)
            keep = [
                piece
                for piece, size in zip(pieces, sizes, strict=True)
                if biggest <= 0 or size >= opts.floater_frac * biggest
            ]
            out.dropped_components = len(pieces) - len(keep)
            if out.dropped_components:
                out.notes.append(
                    f"dropped {out.dropped_components} disconnected piece(s) under "
                    f"{opts.floater_frac:.0%} of the body"
                )
            working = trimesh.util.concatenate(keep) if len(keep) > 1 else keep[0]

    if len(working.faces) > opts.max_triangles:
        before = len(working.faces)
        try:
            working = working.simplify_quadric_decimation(opts.max_triangles)
            out.notes.append(f"decimated {before} triangles to {len(working.faces)}")
        except Exception as exc:  # pragma: no cover - backend-dependent
            out.notes.append(f"could not decimate ({exc}); left at {before} triangles")

    # Lossy rungs are allowed here and refused on the parametric path, because
    # here there is no STEP file for a patched mesh to disagree with.
    repaired = repair(working, allow_lossy=opts.allow_lossy_repair)
    working = repaired.mesh
    out.repair = repaired.as_dict()
    if repaired.changed:
        out.notes.append(repaired.summary())

    if opts.size_mm > 0:
        axis = {"x": 0, "y": 1, "z": 2}[opts.size_axis]
        extent = float(np.ptp(working.vertices[:, axis]))
        if extent <= 0:
            raise ReconstructError(f"the mesh is flat along {opts.size_axis}")
        factor = opts.size_mm / extent
        working.apply_scale(factor)
        out.notes.append(
            f"scaled by {factor:.4g} so {opts.size_axis} measures {opts.size_mm} mm"
        )

    # Seat it on the plate: slicers assume z=0 is the bed, and a model floating
    # above it or sunk below is a support disaster nobody asked for.
    bounds = working.bounds
    working.apply_translation(
        [
            -(bounds[0][0] + bounds[1][0]) / 2,
            -(bounds[0][1] + bounds[1][1]) / 2,
            -bounds[0][2],
        ]
    )

    out.mesh = working
    return out


def reconstruct(images: list[Path], opts: ReconstructOptions) -> ReconstructResult:
    """Send views to the backend and return a cleaned, scaled mesh."""
    if not images:
        raise ReconstructError("no images given")
    if opts.size_mm <= 0:
        raise ReconstructError(
            "a reconstructed mesh has no units: nothing in it says millimetres. "
            "Give the real size of the object with --size (and --axis if it is "
            "not the height), so what comes out is a model of something rather "
            "than a shape."
        )

    payload, suffix = fetch_mesh(images, opts)
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / f"raw{suffix}"
        raw.write_bytes(payload)
        try:
            loaded = trimesh.load(str(raw), force="mesh", process=True)
        except Exception as exc:
            raise ReconstructError(
                f"the backend's {suffix} payload did not load as a mesh: {exc}"
            ) from exc
    if not isinstance(loaded, trimesh.Trimesh) or loaded.faces.size == 0:
        raise ReconstructError("the backend returned something that is not a mesh")

    result = clean(loaded, opts)
    result.backend = opts.url
    return result
