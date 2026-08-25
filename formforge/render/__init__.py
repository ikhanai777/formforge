"""Render service (spec section 6.4).

Produces the six orthographic views, an isometric hero shot and a section cut
that the agent loop shows back to the model. The visual critique is the step
that catches the failures every numeric check passes -- mirrored text, a feature
placed on the wrong face, a proportion that is dimensionally correct and
obviously wrong (spec section 5.4) -- so the renders exist to be *read*, not to
look good.

Two details matter more than fidelity:

* **The build-plate grid.** A 10 mm grid under the isometric view gives the
  vision model an absolute scale reference. Without it a keychain and a coffee
  table are the same picture, and "this is 400 mm long" is invisible.
* **The section cut**, in a colour used nowhere else. Hollow parts hide their
  problems from the outside: a wall that thins to nothing, a cavity that never
  got subtracted, a rib that stops short.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import trimesh

from .raster import (
    BACKGROUND_RGB,
    CLAY_RGB,
    GRID_MAJOR_RGB,
    GRID_RGB,
    SECTION_RGB,
    Camera,
    Framebuffer,
    draw_line,
    rasterize,
    shade,
    tile,
    write_png,
)

__all__ = [
    "RenderResult",
    "ViewSpec",
    "STANDARD_VIEWS",
    "CRITIQUE_VIEWS",
    "render_views",
]

# Direction each named view looks *from*, in model space.
_VIEW_DIRECTIONS: dict[str, tuple[float, float, float]] = {
    "front": (0.0, -1.0, 0.0),
    "back": (0.0, 1.0, 0.0),
    "left": (-1.0, 0.0, 0.0),
    "right": (1.0, 0.0, 0.0),
    "top": (0.0, 0.0, 1.0),
    "bottom": (0.0, 0.0, -1.0),
    "iso": (0.78, -0.78, 0.62),
}

STANDARD_VIEWS = ("front", "back", "left", "right", "top", "bottom", "iso", "section")

# The subset shown to the vision model by default. Six orthographic views cost
# tokens without adding much: the iso view carries the shape, the front and top
# carry the placement of features, and the section carries what the others
# cannot show.
CRITIQUE_VIEWS = ("iso", "front", "top", "section")


@dataclass
class ViewSpec:
    """One camera setup."""

    name: str
    direction: tuple[float, float, float]
    orthographic: bool = True
    # Axis index (0=X, 1=Y, 2=Z) to cut through the centre, or None.
    section_axis: int | None = None
    show_grid: bool = False


def _spec_for(name: str, mesh: trimesh.Trimesh | None = None) -> ViewSpec:
    """Resolve a view name, including the `section` forms."""
    if name == "section" or name.startswith("section_"):
        axis = _section_axis(name, mesh)
        # The cut keeps the negative half, so the camera has to sit on the
        # positive side and look back along the axis -- straight at the exposed
        # cross-section. Viewing a section from any other angle shows the
        # outside of the remaining half, which is what the other seven views
        # already show.
        direction = [0.0, 0.0, 0.0]
        direction[axis] = 1.0
        return ViewSpec(name, tuple(direction), orthographic=True, section_axis=axis)

    direction = _VIEW_DIRECTIONS.get(name)
    if direction is None:
        raise ValueError(
            f"unknown view {name!r}; known views: {', '.join(sorted(_VIEW_DIRECTIONS))} "
            "plus section_x, section_y, section_z"
        )
    return ViewSpec(
        name,
        direction,
        orthographic=name != "iso",
        show_grid=name == "iso",
    )


def _section_axis(name: str, mesh: trimesh.Trimesh | None) -> int:
    """Which axis to cut along.

    An explicit `section_x` names it. A bare `section` picks the shorter of the
    two horizontal axes, so the camera looks through the part's widest face and
    sees the longest possible cross-section. For a pot that is the cut that
    shows the wall running from rim to floor; cutting the other way shows a
    narrow slice that reveals much less.
    """
    if name != "section":
        letter = name.rsplit("_", 1)[-1].lower()
        axis = {"x": 0, "y": 1, "z": 2}.get(letter)
        if axis is None:
            raise ValueError(
                f"unknown section axis in view {name!r}; expected section_x, "
                "section_y, section_z or plain section"
            )
        return axis
    if mesh is None:
        return 1
    extents = np.asarray(mesh.extents, dtype=float)
    return 0 if extents[0] <= extents[1] else 1


@dataclass
class RenderResult:
    """Rendered image paths, plus the contact sheet if one was made."""

    views: dict[str, str]
    contact_sheet: str | None = None
    size: int = 512

    def as_dict(self) -> dict:
        payload: dict = {"views": self.views, "size": self.size}
        if self.contact_sheet:
            payload["contact_sheet"] = self.contact_sheet
        return payload

    def paths(self) -> list[Path]:
        return [Path(p) for p in self.views.values()]


def render_views(
    mesh_path: str | Path,
    out_dir: str | Path,
    *,
    views: tuple[str, ...] | list[str] = CRITIQUE_VIEWS,
    size: int = 512,
    show_build_plate_grid: bool = True,
    make_contact_sheet: bool = True,
    prefix: str = "preview",
) -> RenderResult:
    """Render a mesh from several viewpoints into `out_dir`."""
    mesh = trimesh.load(str(mesh_path), force="mesh", process=True)
    if not isinstance(mesh, trimesh.Trimesh):
        raise ValueError(f"{mesh_path} did not load as a single mesh")
    mesh.merge_vertices()

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    rendered: dict[str, str] = {}
    arrays: list[np.ndarray] = []
    for name in views:
        spec = _spec_for(name, mesh)
        buffer = _render_one(mesh, spec, size, show_build_plate_grid)
        path = out / f"{prefix}_{name}.png"
        buffer.to_png(path)
        rendered[name] = str(path)
        arrays.append(buffer.to_array())

    sheet_path: str | None = None
    if make_contact_sheet and arrays:
        columns = 2 if len(arrays) <= 4 else 3
        sheet = tile(arrays, columns)
        sheet_file = out / f"{prefix}_sheet.png"
        write_png(sheet_file, sheet)
        sheet_path = str(sheet_file)

    return RenderResult(views=rendered, contact_sheet=sheet_path, size=size)


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _render_one(
    mesh: trimesh.Trimesh, spec: ViewSpec, size: int, show_grid: bool
) -> Framebuffer:
    subject, cut_face_mask = _prepare(mesh, spec)

    bounds = np.asarray(mesh.bounds, dtype=float)
    center = bounds.mean(axis=0)
    radius = float(np.linalg.norm(bounds[1] - bounds[0])) / 2.0 or 1.0

    direction = np.asarray(spec.direction, dtype=float)
    direction = direction / (np.linalg.norm(direction) or 1.0)
    distance = radius * 4.0
    eye = center + direction * distance
    up = np.array([0.0, 0.0, 1.0]) if abs(direction[2]) < 0.95 else np.array([0.0, 1.0, 0.0])

    camera = Camera(
        eye=eye,
        target=center,
        up=up,
        orthographic=spec.orthographic,
        # 1.12 leaves a small margin so the part never touches the frame edge,
        # which would make it ambiguous whether it was cropped.
        scale_mm=radius * 1.12,
        fov_deg=32.0,
    )

    buffer = Framebuffer(size, size, BACKGROUND_RGB)
    view = camera.view_matrix()

    if len(subject.faces) > 0:
        vertices = np.asarray(subject.vertices, dtype=float)
        homogeneous = np.hstack([vertices, np.ones((len(vertices), 1))])
        view_space = (homogeneous @ view.T)[:, :3]
        screen = camera.project(view_space, size, size)

        face_normals = np.asarray(subject.face_normals, dtype=float)
        normals_view = face_normals @ view[:3, :3].T
        colors = shade(normals_view, CLAY_RGB)
        if cut_face_mask is not None and cut_face_mask.any():
            # The cut face is lit flat rather than shaded. Its normals point
            # directly away from the camera -- that is what makes it the cut
            # face -- so Lambertian shading renders it almost black, which is
            # the opposite of what a section view is for. A flat bright fill in
            # a colour nothing else uses makes "this is solid material" and
            # "this is the hollow behind it" unmistakable.
            colors[cut_face_mask] = np.asarray(SECTION_RGB, dtype=np.float32)

        # Backface culling is off for a section view: the cut opens the solid,
        # and culling would show straight through it to the background.
        rasterize(
            buffer,
            screen,
            np.asarray(subject.faces, dtype=int),
            colors,
            cull_backfaces=spec.section_axis is None,
        )

    if show_grid and (spec.show_grid or spec.name == "iso"):
        _draw_build_plate(buffer, camera, bounds, size)

    return buffer


def _prepare(
    mesh: trimesh.Trimesh, spec: ViewSpec
) -> tuple[trimesh.Trimesh, np.ndarray | None]:
    """Apply a section cut if the view calls for one.

    Returns the mesh to draw and a mask of the faces that make up the cut
    surface, so they can be coloured differently. Identifying the cap by the
    faces added during the cut is more reliable than testing which faces lie in
    the cutting plane, because a model with a genuine flat face on that plane
    would be mis-coloured by the geometric test.
    """
    if spec.section_axis is None:
        return mesh, None

    axis = spec.section_axis
    normal = np.zeros(3)
    normal[axis] = 1.0
    origin = np.asarray(mesh.bounds, dtype=float).mean(axis=0)

    sliced = None
    try:
        sliced = mesh.slice_plane(origin, -normal, cap=True)
    except Exception:
        # Capping needs shapely. Without it, fall back to an uncapped cut rather
        # than to the uncut mesh: an open cut still shows the interior (backface
        # culling is off for section views), whereas returning the whole model
        # would silently render an ordinary view under a section view's name --
        # and the vision critique would report on a picture of the outside while
        # believing it had seen inside.
        try:
            sliced = mesh.slice_plane(origin, -normal, cap=False)
        except Exception:
            return mesh, None
    if sliced is None or len(sliced.faces) == 0:
        return mesh, None

    # The cap is every face lying exactly in the cutting plane and normal to it.
    # Slicing rebuilds the mesh rather than appending to it, so there is no
    # index range to identify the new faces by. The one false positive this can
    # produce -- a model with a genuine flat face precisely on the cut plane --
    # colours a real face as cut surface, which is a cosmetic error in a
    # diagnostic view and not worth a more elaborate test.
    centers = np.asarray(sliced.triangles_center, dtype=float)
    normals = np.asarray(sliced.face_normals, dtype=float)
    mask = (np.abs(centers[:, axis] - origin[axis]) < 1e-6) & (
        np.abs(normals[:, axis]) > 0.99
    )
    return sliced, mask


def _draw_build_plate(
    buffer: Framebuffer, camera: Camera, bounds: np.ndarray, size: int
) -> None:
    """A 10 mm grid on the z = 0 plane, under and around the part.

    This is the absolute scale cue. A vision model shown an unlabelled render
    has no way to tell a 40 mm keychain from a 400 mm one; against a 10 mm grid
    it can simply count.
    """
    extent = float(max(bounds[1][0] - bounds[0][0], bounds[1][1] - bounds[0][1]))
    span = max(30.0, math.ceil(extent * 0.75 / 10.0) * 10.0)
    center_x = float((bounds[0][0] + bounds[1][0]) / 2)
    center_y = float((bounds[0][1] + bounds[1][1]) / 2)
    # Snap the grid origin to the 10 mm lattice so lines land on round numbers.
    x0 = math.floor((center_x - span) / 10.0) * 10.0
    x1 = math.ceil((center_x + span) / 10.0) * 10.0
    y0 = math.floor((center_y - span) / 10.0) * 10.0
    y1 = math.ceil((center_y + span) / 10.0) * 10.0
    z = float(bounds[0][2])

    view = camera.view_matrix()

    def project(points: np.ndarray) -> np.ndarray:
        homogeneous = np.hstack([points, np.ones((len(points), 1))])
        return camera.project((homogeneous @ view.T)[:, :3], size, size)

    lines: list[tuple[np.ndarray, np.ndarray, tuple[float, float, float]]] = []
    steps_x = int(round((x1 - x0) / 10.0))
    steps_y = int(round((y1 - y0) / 10.0))
    if steps_x > 200 or steps_y > 200:
        return

    for i in range(steps_x + 1):
        x = x0 + i * 10.0
        # Every fifth line (50 mm) is emphasised so a viewer can count in fives.
        rgb = GRID_MAJOR_RGB if abs(x) % 50 < 1e-6 else GRID_RGB
        lines.append((np.array([[x, y0, z]]), np.array([[x, y1, z]]), rgb))
    for j in range(steps_y + 1):
        y = y0 + j * 10.0
        rgb = GRID_MAJOR_RGB if abs(y) % 50 < 1e-6 else GRID_RGB
        lines.append((np.array([[x0, y, z]]), np.array([[x1, y, z]]), rgb))

    for start, end, rgb in lines:
        a = project(start)[0]
        b = project(end)[0]
        draw_line(buffer, a, b, rgb)
