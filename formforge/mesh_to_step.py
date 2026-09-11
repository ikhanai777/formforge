"""Convert an existing mesh (STL) into a STEP (AP214) file.

FormForge's own pipeline gets STEP for free (spec section 3.2, `bundle.py`): a
parametric build123d solid already has real BREP surfaces, so exporting to
STEP just serializes them. This module is for the opposite direction -- an
STL that came from somewhere else (a scan, a download, another tool) and
needs to open in software that only speaks STEP.

There is no way to recover the analytic surfaces (planes, cylinders, fillets)
that produced an arbitrary mesh; that information is gone once triangulated.
What this does instead is turn every triangle into a real planar BRep face,
sew them into a shell, and, if the mesh is watertight, close the shell into a
solid -- a "faceted BREP" that exactly reproduces the mesh's geometry (no
smoothing, no approximation) in a form AP214 readers can open. Adjacent
triangles that are exactly coplanar are merged into a single face afterwards;
that step is lossless (it only merges faces that share a surface) and, for
STLs that were themselves exported from CAD (mostly flat), removes most of
the triangle-per-face bloat. It does nothing for organic/scanned meshes,
where no two triangles are exactly coplanar.

This is an interop utility, not a substitute for real parametric CAD: curved
and filleted surfaces stay faceted, and the result will not be editable the
way a build123d-authored STEP is.

Unlike the rest of formforge, this imports build123d (and OCCT through it)
directly rather than through the geometry sandbox in `formforge/sandbox/`.
That sandbox isolates *LLM-authored* code of unknown intent; this module runs
a single fixed, trusted routine over a mesh the user already has on disk, the
same trust level as `formforge check` or `formforge render` -- so it does not
need a subprocess boundary, only the triangle-count guard below.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import trimesh

# Each triangle costs one BRepBuilderAPI_MakePolygon + MakeFace call, plus its
# share of the sewing pass; empirically that is roughly 1ms/triangle end to
# end. Above this the conversion is minutes, not seconds -- bound it rather
# than let a big STL hang the CLI with no feedback.
MAX_TRIANGLES = 50_000

# STL files carry no unit; this is the caller's declaration of what the
# coordinates mean, so the STEP file (always written in mm, formforge's
# convention throughout) is dimensionally correct rather than merely labelled.
MM_PER_UNIT = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "ft": 304.8}


@dataclass
class ConversionResult:
    """What the conversion produced, for the CLI and for callers that check."""

    input_path: str
    output_path: str
    triangle_count: int
    dropped_degenerate: int
    face_count: int
    is_manifold: bool
    volume_mm3: float
    duration_s: float
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return dict(self.__dict__)

    def summary_line(self) -> str:
        shape = "solid" if self.is_manifold else "open shell"
        volume = f", {self.volume_mm3:.1f} mm3" if self.is_manifold else ""
        return (
            f"{self.triangle_count} triangles -> {self.face_count} BREP faces "
            f"({shape}{volume}) in {self.duration_s:.1f}s"
        )


def convert_stl_to_step(
    input_path: str | Path,
    output_path: str | Path,
    *,
    input_unit: str = "mm",
    merge_coplanar: bool = True,
    max_triangles: int = MAX_TRIANGLES,
) -> ConversionResult:
    """Convert the mesh at `input_path` into a faceted-BREP STEP file.

    Raises ValueError for anything that is the input's fault (unreadable
    file, no triangles, over the triangle cap) so the CLI can print it
    without a traceback.
    """
    from build123d import Face, Shell, Solid, Unit, Vector, Wire, export_step  # noqa: PLC0415

    input_path = Path(input_path)
    output_path = Path(output_path)

    if input_unit not in MM_PER_UNIT:
        raise ValueError(f"input_unit must be one of {sorted(MM_PER_UNIT)}, got {input_unit!r}")

    try:
        mesh = trimesh.load(input_path, force="mesh")
    except Exception as exc:
        raise ValueError(f"could not read {input_path} as a mesh: {exc}") from exc

    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
        raise ValueError(f"{input_path} has no triangles to convert")

    good = mesh.nondegenerate_faces()
    dropped = int(len(mesh.faces) - good.sum())
    if dropped:
        mesh.update_faces(good)
    if len(mesh.faces) == 0:
        raise ValueError(f"{input_path} has no non-degenerate triangles to convert")

    if len(mesh.faces) > max_triangles:
        raise ValueError(
            f"{input_path} has {len(mesh.faces)} triangles, over the {max_triangles} limit "
            "-- decimate it first (e.g. trimesh's `simplify_quadric_decimation`) or pass "
            "a higher max_triangles once you have accepted the export time"
        )

    scale = MM_PER_UNIT[input_unit]
    vertices = mesh.vertices * scale if scale != 1.0 else mesh.vertices

    start = time.perf_counter()

    faces = [
        Face(Wire.make_polygon([Vector(*vertices[i]) for i in tri], close=True))
        for tri in mesh.faces
    ]
    shell = Shell(faces)
    is_manifold = shell.is_manifold

    warnings: list[str] = []
    if dropped:
        warnings.append(f"dropped {dropped} degenerate (zero-area) triangle(s)")
    if not is_manifold:
        warnings.append(
            "mesh is not watertight; wrote an open shell instead of a solid -- "
            "some CAD tools will refuse to import it"
        )

    to_export = Solid(shell) if is_manifold else shell
    if merge_coplanar and is_manifold:
        to_export = _unify_coplanar(to_export)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    export_step(to_export, output_path, unit=Unit.MM)

    return ConversionResult(
        input_path=str(input_path),
        output_path=str(output_path),
        triangle_count=len(mesh.faces),
        dropped_degenerate=dropped,
        face_count=len(to_export.faces()),
        is_manifold=is_manifold,
        volume_mm3=float(to_export.volume) if is_manifold else 0.0,
        duration_s=time.perf_counter() - start,
        warnings=warnings,
    )


def _unify_coplanar(solid):
    """Merge adjacent triangle-faces that share an exact plane into one face.

    `ShapeUpgrade_UnifySameDomain` only merges faces with identical
    underlying surfaces, so this is a pure size reduction, not an
    approximation -- the boundary and volume are unchanged.
    """
    from build123d import Solid  # noqa: PLC0415
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain  # noqa: PLC0415
    from OCP.TopoDS import TopoDS  # noqa: PLC0415

    unifier = ShapeUpgrade_UnifySameDomain(solid.wrapped, True, True, True)
    unifier.Build()
    return Solid(TopoDS.Solid_s(unifier.Shape()))
