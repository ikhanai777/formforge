"""Height field to solid: the mesh builder.

A tile is the volume between a flat back and a sampled top surface, clipped to a
footprint polygon and with its back features cut out of it. Building it directly
rather than through the CAD kernel is a deliberate exception to the rule the
rest of FormForge follows, and the reason is that a relief surface is not a
parametric object. A 300 x 200 mm dune field at 0.4 mm sampling is 375 000
distinct heights; there is no B-rep formulation of that which is not simply the
mesh with extra steps, and OCCT would spend minutes tessellating a surface that
numpy produces exactly, at the sample spacing that was asked for, in
milliseconds.

What is *not* given up is watertightness. The sandwich is closed by
construction:

    top surface (nx x ny grid)  +  flat back  +  four skirt walls

Every vertex on the boundary is shared between the top and its skirt, and every
skirt vertex is shared with the back, so there are no gaps to seal and no repair
pass to run. The only operations that can break that are the boolean clips --
the footprint and the back pockets -- and each one is checked immediately
afterwards rather than at export time, because a boolean that silently produced
an open mesh is worth catching next to the operation that caused it.

The footprint and the pockets go through manifold3d (already a dependency, via
trimesh). Exact interlocking joinery is the whole point of the tiling feature,
and a stair-stepped dovetail flank sampled onto the height grid would not slide
into its socket.
"""

from __future__ import annotations

import math

import numpy as np
import trimesh

from .field import Bounds, PatternField

__all__ = [
    "SurfaceError",
    "grid_shape",
    "heightfield_solid",
    "intersect_prism",
    "prism",
    "subtract",
]


class SurfaceError(RuntimeError):
    """A mesh operation produced something that is not a printable solid."""


def grid_shape(
    bounds: Bounds, pitch_mm: float, max_samples: int | None = None
) -> tuple[int, int, float]:
    """Sample counts and the actual pitch used for a window.

    Returns at least a 2 x 2 grid: one quad still makes a valid (if flat) solid,
    and a degenerate 1 x n grid has no faces at all. If `max_samples` is given
    the pitch is relaxed until the grid fits under it, and the caller is told
    the pitch that was really used rather than the one that was asked for --
    silently returning a coarser surface than requested is how a panel comes
    out looking different from its preview.
    """
    if pitch_mm <= 0:
        raise ValueError("sample pitch must be positive")
    nx = max(round(bounds.width / pitch_mm) + 1, 2)
    ny = max(round(bounds.height / pitch_mm) + 1, 2)
    if max_samples is not None and nx * ny > max_samples:
        # Scale both axes by the same factor so the samples stay square.
        factor = math.sqrt(nx * ny / max_samples)
        nx = max(int(nx / factor), 2)
        ny = max(int(ny / factor), 2)
    effective = bounds.width / (nx - 1) if nx > 1 else pitch_mm
    return nx, ny, effective


def heightfield_solid(
    field: PatternField,
    bounds: Bounds,
    *,
    base_mm: float,
    relief_mm: float,
    pitch_mm: float,
    layer_mm: float | None = None,
    max_samples: int | None = None,
) -> tuple[trimesh.Trimesh, float]:
    """Build the closed solid for one window onto the field.

    The returned mesh sits in *world* coordinates in X and Y -- it is not moved
    to the origin -- because a tile's whole identity is where on the panel it
    came from, and moving it is the caller's job at export time. Z runs from 0
    (the back, flat on the build plate) to `base_mm + relief_mm`.
    """
    if base_mm <= 0:
        raise ValueError("base thickness must be positive")
    if relief_mm < 0:
        raise ValueError("relief depth cannot be negative")

    nx, ny, effective_pitch = grid_shape(bounds, pitch_mm, max_samples)
    heights = field.sample_grid(bounds, nx, ny)
    z_top = base_mm + heights * relief_mm

    if layer_mm and layer_mm > 0:
        # Snap the relief to whole layers. The printer is going to do this
        # anyway; doing it here means the mesh, the preview and the print all
        # agree, and it removes any sub-layer film left in the pattern's floor.
        z_top = base_mm + np.round((z_top - base_mm) / layer_mm) * layer_mm

    xs = np.linspace(bounds.x0, bounds.x1, nx)
    ys = np.linspace(bounds.y0, bounds.y1, ny)
    gx, gy = np.meshgrid(xs, ys, indexing="ij")

    count = nx * ny
    top = np.stack([gx.ravel(), gy.ravel(), z_top.ravel()], axis=-1)
    back = np.stack([gx.ravel(), gy.ravel(), np.zeros(count)], axis=-1)
    vertices = np.vstack([top, back])

    index = np.arange(count).reshape(nx, ny)
    a = index[:-1, :-1].ravel()
    b = index[1:, :-1].ravel()
    c = index[1:, 1:].ravel()
    d = index[:-1, 1:].ravel()

    # Top: (a, b, c) and (a, c, d) wind counter-clockwise seen from +Z, so both
    # normals point up and out of the solid.
    faces = [np.stack([a, b, c], axis=-1), np.stack([a, c, d], axis=-1)]
    # Back: the same quads with the winding reversed, so they face -Z.
    faces += [
        np.stack([a + count, c + count, b + count], axis=-1),
        np.stack([a + count, d + count, c + count], axis=-1),
    ]
    # Skirts. Whether a wall needs flipping follows from which way its edge runs
    # relative to the outward direction; see the note in the module docstring
    # about the sandwich being closed by construction.
    faces.append(_skirt(index[0, :], count, flip=False))  # x = min, faces -X
    faces.append(_skirt(index[-1, :], count, flip=True))  # x = max, faces +X
    faces.append(_skirt(index[:, 0], count, flip=True))  # y = min, faces -Y
    faces.append(_skirt(index[:, -1], count, flip=False))  # y = max, faces +Y

    mesh = trimesh.Trimesh(vertices, np.vstack(faces), process=False)
    _require_solid(mesh, "the height field surface")
    return mesh, effective_pitch


def _skirt(line: np.ndarray, count: int, *, flip: bool) -> np.ndarray:
    """Two triangles per segment, joining a boundary run of the top to the back."""
    top_a, top_b = line[:-1], line[1:]
    back_a, back_b = top_a + count, top_b + count
    quad = np.vstack(
        [
            np.stack([top_a, top_b, back_b], axis=-1),
            np.stack([top_a, back_b, back_a], axis=-1),
        ]
    )
    return quad[:, ::-1] if flip else quad


def prism(polygon, *, z0: float, z1: float) -> trimesh.Trimesh:
    """A shapely polygon extruded between two Z heights."""
    if z1 <= z0:
        raise ValueError("prism needs z1 > z0")
    solid = trimesh.creation.extrude_polygon(polygon, height=z1 - z0)
    solid.apply_translation([0.0, 0.0, z0])
    return solid


def intersect_prism(mesh: trimesh.Trimesh, polygon, *, margin: float = 5.0) -> trimesh.Trimesh:
    """Clip a solid to a footprint polygon.

    The cutting prism is extended `margin` beyond the mesh in Z so the clip only
    ever touches the sides. A prism that ends exactly on the top surface would
    put a boolean seam along it, and coplanar boolean faces are where mesh
    kernels produce their worst results.
    """
    z0 = float(mesh.bounds[0][2]) - margin
    z1 = float(mesh.bounds[1][2]) + margin
    result = trimesh.boolean.intersection(
        [mesh, prism(polygon, z0=z0, z1=z1)], engine="manifold"
    )
    result = _as_single(result, "clipping the tile to its footprint")
    _require_solid(result, "the footprint clip")
    return result


def subtract(
    mesh: trimesh.Trimesh, cutters: list[trimesh.Trimesh], what: str
) -> trimesh.Trimesh:
    """Cut a list of solids out of a mesh, or return it untouched if there are none."""
    if not cutters:
        return mesh
    result = trimesh.boolean.difference([mesh, *cutters], engine="manifold")
    result = _as_single(result, what)
    _require_solid(result, what)
    return result


def _as_single(result, what: str) -> trimesh.Trimesh:
    """Collapse a boolean result to one mesh, or say what fell apart."""
    if isinstance(result, trimesh.Scene):
        meshes = list(result.geometry.values())
    elif isinstance(result, list):
        meshes = result
    else:
        meshes = [result]
    meshes = [m for m in meshes if isinstance(m, trimesh.Trimesh) and len(m.faces) > 0]
    if not meshes:
        raise SurfaceError(
            f"{what} removed the entire solid; check the feature sizes against the tile"
        )
    if len(meshes) > 1:
        raise SurfaceError(
            f"{what} split the tile into {len(meshes)} loose pieces. A pocket or a "
            "joint cut right through it -- reduce the feature depth, or thicken the base."
        )
    return meshes[0]


def _require_solid(mesh: trimesh.Trimesh, what: str) -> None:
    """Fail immediately, and locally, when an operation stops producing a solid."""
    if len(mesh.faces) == 0:
        raise SurfaceError(f"{what} produced an empty mesh")
    if not mesh.is_watertight:
        raise SurfaceError(
            f"{what} produced a mesh with {len(mesh.faces)} faces that is not watertight. "
            "This is a bug in the generator, not in the design: report the pattern, "
            "size and joint that produced it."
        )
    if mesh.volume <= 0:
        raise SurfaceError(
            f"{what} produced a mesh whose faces are wound inside out "
            f"(signed volume {mesh.volume:.3f} mm^3)"
        )


def triangle_budget_pitch(bounds: Bounds, requested_pitch: float, budget: int) -> float:
    """The finest pitch that keeps a window under a triangle budget.

    Two triangles per grid cell on the top and two on the back, so a budget of
    N triangles is about N/4 cells. Capping this matters: the difference between
    0.4 mm and 0.15 mm sampling on a 250 mm tile is 300 000 triangles against
    2.2 million, and the second one is a 200 MB STL that a slicer will struggle
    to open for detail no nozzle can print.
    """
    cells = max(budget // 4, 16)
    area = max(bounds.width * bounds.height, 1e-9)
    finest = math.sqrt(area / cells)
    return max(requested_pitch, finest)
