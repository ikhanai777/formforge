"""A flat drawing of what the tracer decided.

The renders in the bundle show the finished solid. This shows the *decision*:
the outline that survived cleanup, which openings became engraving, where the
hooks and the fixings landed, and -- in outline behind it all -- the raw traced
shape before the minimum-feature pass ate the thin bits. When a key holder comes
out wrong, the answer is almost always visible here and almost never visible in
an isometric render of the result.

It draws with the same numpy-and-write_png approach as the render service rather
than a plotting library, for the same reason: one fewer dependency, and it has
to run in the same minimal image.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import numpy as np

from ..render.raster import write_png
from .design import KeyHolderPlan, mount_footprint

BACKGROUND = (250, 250, 248)
PLATE = (196, 168, 132)
PLATE_EDGE = (120, 96, 66)
ENGRAVED = (150, 122, 88)
HOOK = (92, 116, 140)
MOUNT = (196, 76, 64)
TRACE = (206, 206, 200)


def _rings_of(polygon) -> list[np.ndarray]:
    rings = [np.asarray(polygon.exterior.coords, dtype=np.float64)]
    rings += [np.asarray(ring.coords, dtype=np.float64) for ring in polygon.interiors]
    return rings


def _fill(canvas: np.ndarray, rings: list[np.ndarray], colour: tuple[int, int, int]) -> None:
    """Even-odd scanline fill. Holes come for free by including their rings."""
    if not rings:
        return
    height, width = canvas.shape[:2]
    edges = []
    for ring in rings:
        if len(ring) < 3:
            continue
        closed = np.vstack([ring, ring[:1]])
        edges.append(np.hstack([closed[:-1], closed[1:]]))
    if not edges:
        return
    segments = np.vstack(edges)
    x0, y0, x1, y1 = segments[:, 0], segments[:, 1], segments[:, 2], segments[:, 3]
    top = np.minimum(y0, y1)
    bottom = np.maximum(y0, y1)

    row_start = max(0, int(np.floor(top.min()).item()))
    row_end = min(height - 1, int(np.ceil(bottom.max()).item()))
    for row in range(row_start, row_end + 1):
        y = row + 0.5
        hit = (top <= y) & (bottom > y)
        if not hit.any():
            continue
        ax, ay, bx, by = x0[hit], y0[hit], x1[hit], y1[hit]
        crossings = np.sort(ax + (y - ay) / (by - ay) * (bx - ax))
        for start, end in zip(crossings[0::2], crossings[1::2], strict=False):
            left = max(0, int(np.ceil(start - 0.5)))
            right = min(width - 1, int(np.floor(end - 0.5)))
            if right >= left:
                canvas[row, left : right + 1] = colour


def _outline(canvas: np.ndarray, rings: list[np.ndarray], colour, width: int = 1) -> None:
    height, image_width = canvas.shape[:2]
    for ring in rings:
        if len(ring) < 2:
            continue
        closed = np.vstack([ring, ring[:1]])
        for (ax, ay), (bx, by) in pairwise(closed):
            steps = int(max(abs(bx - ax), abs(by - ay))) + 1
            xs = np.linspace(ax, bx, steps)
            ys = np.linspace(ay, by, steps)
            for dx in range(-width + 1, width):
                for dy in range(-width + 1, width):
                    cx = np.clip((xs + dx).astype(int), 0, image_width - 1)
                    cy = np.clip((ys + dy).astype(int), 0, height - 1)
                    canvas[cy, cx] = colour


def render_trace(
    plan: KeyHolderPlan,
    path: str | Path,
    *,
    size: int = 700,
    raw_outline: list[tuple[float, float]] | None = None,
) -> Path:
    """Draw the planned key holder flat, as seen from the front."""
    width_mm, height_mm, _ = plan.bounding_box_mm()
    margin_mm = max(6.0, width_mm * 0.04)
    span_x = width_mm + 2 * margin_mm
    span_y = height_mm + 2 * margin_mm
    scale = size / max(span_x, span_y)
    image_w = max(32, round(span_x * scale))
    image_h = max(32, round(span_y * scale))

    canvas = np.zeros((image_h, image_w, 3), dtype=np.uint8)
    canvas[:, :] = BACKGROUND

    def to_px(points) -> np.ndarray:
        array = np.asarray(points, dtype=np.float64)
        xs = (array[:, 0] + margin_mm) * scale
        # Image rows run downwards; the model's Y runs up the wall.
        ys = image_h - (array[:, 1] + margin_mm) * scale
        return np.column_stack([xs, ys])

    if raw_outline:
        _outline(canvas, [to_px(raw_outline)], TRACE, width=1)

    plate_rings = [to_px(plan.outline)] + [to_px(ring) for ring in plan.holes]
    _fill(canvas, plate_rings, PLATE)
    for ring in plan.engrave:
        _fill(canvas, [to_px(ring)], ENGRAVED)
    _outline(canvas, [to_px(plan.outline)], PLATE_EDGE, width=1)

    # Hooks, drawn as their footprint on the rail plus their projection: the
    # front view cannot show how far they stick out, so the tick below each one
    # is drawn to scale in the same millimetres.
    for x in plan.hook_x_mm:
        half = plan.spec.hook_w_mm / 2.0
        y0 = plan.hook_base_y_mm
        y1 = y0 + plan.spec.hook_root_h_mm
        corners = [(x - half, y0), (x + half, y0), (x + half, y1), (x - half, y1)]
        _fill(canvas, [to_px(corners)], HOOK)

    for mount in plan.mounts:
        footprint = mount_footprint(plan.spec, mount.x_mm, mount.y_mm)
        _outline(canvas, [to_px(ring) for ring in _rings_of(footprint)], MOUNT, width=2)

    return write_png(path, canvas)
