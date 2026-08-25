"""Mesh repair, as an ordered escalation from free to destructive (spec 7.4).

The central rule, and the reason this module is small rather than clever:

    On the parametric path, prefer regenerating over repairing.

A broken boolean is a *logic* bug. Patching the mesh hides it, and worse, it
makes the STL and the STEP file disagree -- the user downloads a part that
prints and a CAD file that does not match it, and nothing in the system knows.

So the ladder is split in two. Lossless rungs change no geometry and run
automatically; lossy rungs change the shape and must be asked for explicitly,
and are refused outright on the parametric path.

The distinction is not academic. Welding a vertex or dropping a zero-area
triangle provably does not move a single surface point. Filling a hole invents
surface that the CAD model never described.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import trimesh

# Area below which a triangle carries no geometry and can be dropped.
DEGENERATE_AREA_MM2 = 1e-9

# Boundary loops longer than this are not incidental tessellation damage; they
# are a genuinely missing surface, and filling them would be invention.
MAX_FILLABLE_LOOP_EDGES = 20


@dataclass
class RepairStep:
    """One rung attempted, and what it changed."""

    name: str
    applied: bool
    lossless: bool
    detail: str
    faces_before: int = 0
    faces_after: int = 0

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "applied": self.applied,
            "lossless": self.lossless,
            "detail": self.detail,
            "faces_before": self.faces_before,
            "faces_after": self.faces_after,
        }


@dataclass
class RepairResult:
    """The repaired mesh plus a full account of what was done to it."""

    mesh: trimesh.Trimesh
    steps: list[RepairStep] = field(default_factory=list)
    watertight_before: bool = False
    watertight_after: bool = False
    volume_change_mm3: float = 0.0

    @property
    def changed(self) -> bool:
        return any(step.applied for step in self.steps)

    @property
    def lossless(self) -> bool:
        """Did every applied step provably preserve the geometry?"""
        return all(step.lossless for step in self.steps if step.applied)

    @property
    def fixed(self) -> bool:
        return self.watertight_after and not self.watertight_before

    def as_dict(self) -> dict:
        return {
            "changed": self.changed,
            "lossless": self.lossless,
            "fixed": self.fixed,
            "watertight_before": self.watertight_before,
            "watertight_after": self.watertight_after,
            "volume_change_mm3": round(self.volume_change_mm3, 6),
            "steps": [s.as_dict() for s in self.steps if s.applied],
        }

    def summary(self) -> str:
        applied = [s for s in self.steps if s.applied]
        if not applied:
            return "no repair needed"
        names = ", ".join(s.name for s in applied)
        suffix = " (geometry preserved)" if self.lossless else " (GEOMETRY MODIFIED)"
        return f"repaired: {names}{suffix}"


def repair(
    mesh: trimesh.Trimesh,
    *,
    allow_lossy: bool = False,
) -> RepairResult:
    """Run the repair ladder, stopping as soon as the mesh is sound.

    With `allow_lossy=False` (the default, and the only correct setting for the
    parametric path) only the geometry-preserving rungs run. If those are not
    enough, the mesh comes back still broken -- which is the right outcome,
    because the caller should regenerate rather than accept a mesh that no
    longer matches its own source code.
    """
    working = mesh.copy()
    result = RepairResult(mesh=working, watertight_before=bool(mesh.is_watertight))
    volume_before = float(mesh.volume) if mesh.is_watertight else 0.0

    _weld(working, result)
    _drop_degenerate(working, result)
    _fix_winding(working, result)

    if not working.is_watertight and allow_lossy:
        _fill_small_holes(working, result)
    if not working.is_watertight and allow_lossy:
        _manifold_rebuild(working, result)

    result.mesh = working
    result.watertight_after = bool(working.is_watertight)
    if result.watertight_after and volume_before:
        result.volume_change_mm3 = float(working.volume) - volume_before
    return result


def repair_file(
    path: str | Path,
    output: str | Path | None = None,
    *,
    allow_lossy: bool = False,
) -> RepairResult:
    """Repair a mesh on disk, writing the result back (or to `output`)."""
    mesh = trimesh.load(str(path), force="mesh", process=True)
    if not isinstance(mesh, trimesh.Trimesh):
        raise ValueError(f"{path} did not load as a single mesh")
    result = repair(mesh, allow_lossy=allow_lossy)
    if result.changed:
        result.mesh.export(str(output or path))
    return result


# ---------------------------------------------------------------------------
# Lossless rungs. These provably do not move any surface point.
# ---------------------------------------------------------------------------


def _weld(mesh: trimesh.Trimesh, result: RepairResult) -> None:
    """Merge coincident vertices so adjacent triangles share topology.

    Lossless: merging two vertices at the same coordinates changes indices, not
    positions.
    """
    before = len(mesh.vertices)
    mesh.merge_vertices()
    after = len(mesh.vertices)
    result.steps.append(
        RepairStep(
            name="weld_vertices",
            applied=after < before,
            lossless=True,
            detail=f"merged {before - after} coincident vertices",
        )
    )


def _drop_degenerate(mesh: trimesh.Trimesh, result: RepairResult) -> None:
    """Remove zero-area triangles.

    Lossless: a triangle with no area occupies no space and bounds nothing.
    These are a normal artifact of tessellating a sphere or a cone, where the
    surface parameterisation collapses at the pole -- two of them are enough to
    make an otherwise perfect mesh read as non-watertight, which is why this
    rung earns its place at the bottom of the ladder rather than being treated
    as an exotic failure.
    """
    areas = np.asarray(mesh.area_faces, dtype=float)
    keep = areas >= DEGENERATE_AREA_MM2
    removed = int(np.count_nonzero(~keep))
    faces_before = len(mesh.faces)
    if removed:
        mesh.update_faces(keep)
        mesh.remove_unreferenced_vertices()
        mesh.merge_vertices()
    result.steps.append(
        RepairStep(
            name="drop_degenerate_faces",
            applied=removed > 0,
            lossless=True,
            detail=f"removed {removed} zero-area face(s)",
            faces_before=faces_before,
            faces_after=len(mesh.faces),
        )
    )


def _fix_winding(mesh: trimesh.Trimesh, result: RepairResult) -> None:
    """Make face winding consistent and normals point outward.

    Lossless: reversing a triangle's vertex order changes which side is
    considered outside, not where the triangle is.
    """
    needed = not (mesh.is_winding_consistent and mesh.volume > 0)
    if needed:
        trimesh.repair.fix_winding(mesh)
        trimesh.repair.fix_inversion(mesh)
        trimesh.repair.fix_normals(mesh)
    result.steps.append(
        RepairStep(
            name="fix_winding",
            applied=needed,
            lossless=True,
            detail="made face winding consistent and normals outward",
        )
    )


# ---------------------------------------------------------------------------
# Lossy rungs. These change the shape. Never used on the parametric path.
# ---------------------------------------------------------------------------


def _fill_small_holes(mesh: trimesh.Trimesh, result: RepairResult) -> None:
    """Cap small boundary loops with new triangles.

    Lossy: the new surface is invented, not derived from the CAD model. Bounded
    to small loops so it can only ever patch tessellation damage, never
    reconstruct a face the model is genuinely missing.
    """
    boundary = _boundary_loop_sizes(mesh)
    fillable = [n for n in boundary if n <= MAX_FILLABLE_LOOP_EDGES]
    if not fillable:
        result.steps.append(
            RepairStep(
                name="fill_holes",
                applied=False,
                lossless=False,
                detail=(
                    f"{len(boundary)} boundary loop(s), largest {max(boundary, default=0)} "
                    "edges: too large to fill without inventing surface"
                ),
            )
        )
        return

    faces_before = len(mesh.faces)
    trimesh.repair.fill_holes(mesh)
    result.steps.append(
        RepairStep(
            name="fill_holes",
            applied=len(mesh.faces) != faces_before,
            lossless=False,
            detail=f"capped {len(fillable)} boundary loop(s)",
            faces_before=faces_before,
            faces_after=len(mesh.faces),
        )
    )


def _manifold_rebuild(mesh: trimesh.Trimesh, result: RepairResult) -> None:
    """Rebuild the mesh through manifold3d, which guarantees a valid solid.

    Lossy: manifold3d resolves self-intersections and non-manifold edges by
    changing the surface. It is topologically guaranteed in a way OCCT's mesh
    booleans are not, which is why it is the right tool for the decorative path
    (spec section 6.6) -- and the wrong one for a dimensioned part.
    """
    try:
        from manifold3d import Manifold, Mesh  # noqa: PLC0415
    except ImportError:
        result.steps.append(
            RepairStep(
                name="manifold_rebuild",
                applied=False,
                lossless=False,
                detail="manifold3d is not installed",
            )
        )
        return

    faces_before = len(mesh.faces)
    try:
        source = Mesh(
            vert_properties=np.asarray(mesh.vertices, dtype=np.float32),
            tri_verts=np.asarray(mesh.faces, dtype=np.uint32),
        )
        rebuilt = Manifold(source).to_mesh()
        mesh.vertices = np.asarray(rebuilt.vert_properties)[:, :3].astype(float)
        mesh.faces = np.asarray(rebuilt.tri_verts, dtype=np.int64)
        applied = True
        detail = "rebuilt through manifold3d"
    except Exception as exc:
        applied = False
        detail = f"manifold3d could not rebuild the mesh: {exc}"

    result.steps.append(
        RepairStep(
            name="manifold_rebuild",
            applied=applied,
            lossless=False,
            detail=detail,
            faces_before=faces_before,
            faces_after=len(mesh.faces),
        )
    )


def _boundary_loop_sizes(mesh: trimesh.Trimesh) -> list[int]:
    """Edge counts of each open boundary loop in the mesh."""
    try:
        edges = mesh.edges_sorted
        unique, counts = np.unique(edges, axis=0, return_counts=True)
        open_edges = unique[counts == 1]
    except Exception:
        return []
    if len(open_edges) == 0:
        return []

    # Walk the open-edge graph to group edges into loops.
    adjacency: dict[int, list[int]] = {}
    for a, b in open_edges:
        adjacency.setdefault(int(a), []).append(int(b))
        adjacency.setdefault(int(b), []).append(int(a))

    seen: set[int] = set()
    loops: list[int] = []
    for start in adjacency:
        if start in seen:
            continue
        stack = [start]
        size = 0
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            size += 1
            stack.extend(n for n in adjacency[node] if n not in seen)
        loops.append(size)
    return loops
