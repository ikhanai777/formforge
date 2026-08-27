"""Image to relief: the hybrid decorative path from architecture.md section 5.

The rule that path has to follow was settled before it was built: functional
geometry is never generated. So everything that decides whether the piece
prints and hangs -- panel thickness, outline, the hanging hole, the relief
depth -- stays parametric and is owned by the emitted script's own constants.
The image contributes one thing only, the silhouette that sits on top, and the
result goes through the same three validation tiers as every template.

That boundary is also what makes the output clean. A single-image mesh
generator infers the side of the object it cannot see and leaves you to
discover the non-manifold edges afterwards. Tracing a silhouette infers
nothing: the contour is exact, it closes by construction, and the solid is a
closed face extruded by a kernel. What it cannot do is give you a back --
this produces a relief, not a figurine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

__all__ = [
    "EmbossOptions",
    "TraceResult",
    "emboss_source",
    "load_mask",
    "trace_polygons",
]


@dataclass
class EmbossOptions:
    """Everything the caller can steer, with print-safe defaults."""

    width_mm: float = 150.0
    relief_mm: float = 2.8
    panel_t_mm: float = 4.0
    margin_mm: float = 10.0
    # No panel at all: cut the silhouette out as a standalone piece. Only safe
    # when the shape is one connected component, which is checked, not assumed.
    standalone: bool = False
    corner_r_mm: float = 8.0
    hang_d_mm: float = 5.0
    hang_inset_mm: float = 9.0
    # Contour handling, in pixels.
    smooth_px: float = 1.8
    simplify_px: float = 0.9
    min_area_frac: float = 0.002
    max_points: int = 900
    threshold: float | None = None
    invert: bool = False


@dataclass
class TraceResult:
    polygons: list = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    point_count: int = 0
    holes: int = 0


# --------------------------------------------------------------------- mask


def load_mask(path: str | Path, opts: EmbossOptions) -> np.ndarray:
    """Reduce an image to the subject silhouette.

    Alpha wins when it carries real transparency, because a cut-out is the
    author telling us exactly what the subject is. Otherwise the background is
    inferred from the border, which is right for product shots and clip art and
    wrong for a photograph whose subject runs off the edge -- hence the
    coverage note the caller gets back.
    """
    try:
        from PIL import Image
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "Reading an image needs Pillow. Install it with "
            "`pip install 'formforge[image]'` to use emboss."
        ) from exc

    img = Image.open(path)
    img = img.convert("RGBA")
    arr = np.asarray(img).astype(np.float32) / 255.0
    alpha = arr[..., 3]

    if alpha.min() < 0.9:
        mask = alpha > 0.5
    else:
        rgb = arr[..., :3]
        lum = rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
        border = np.concatenate([lum[0], lum[-1], lum[:, 0], lum[:, -1]])
        bg = float(np.median(border))
        level = opts.threshold if opts.threshold is not None else 0.5
        # Split away from whatever the border is, rather than at a fixed
        # brightness: dark art on white and white art on black both work.
        mask = (lum < bg - (1.0 - level) * 0.35) if bg > 0.5 else (lum > bg + level * 0.35)

    if opts.invert:
        mask = ~mask
    return mask


def _clean(mask: np.ndarray, opts: EmbossOptions) -> tuple[np.ndarray, list[str]]:
    from scipy import ndimage

    notes: list[str] = []
    if opts.smooth_px > 0:
        soft = ndimage.gaussian_filter(mask.astype(np.float32), opts.smooth_px)
        mask = soft > 0.5

    labels, n = ndimage.label(mask)
    if n == 0:
        return mask, ["the image reduced to an empty silhouette"]

    sizes = ndimage.sum(mask, labels, range(1, n + 1))
    keep = sizes >= opts.min_area_frac * mask.size
    if not keep.any():
        keep[int(np.argmax(sizes))] = True
    dropped = int((~keep).sum())
    if dropped:
        notes.append(f"dropped {dropped} speck(s) below {opts.min_area_frac:.1%} of the frame")
    mask = np.isin(labels, np.nonzero(keep)[0] + 1)

    kept = int(keep.sum())
    if kept > 1:
        notes.append(f"{kept} separate shapes")
    return mask, notes


# ------------------------------------------------------------------ tracing


def _boundary_loops(mask: np.ndarray) -> list[np.ndarray]:
    """Closed pixel-boundary loops, walked from the edges between in and out.

    Exact rather than interpolated: every segment is a real edge of a real
    pixel, so a loop always closes and never self-crosses. Smoothing happens
    before this, on the mask, and simplification after it, on the ring.
    """
    padded = np.pad(mask, 1, constant_values=False)
    edges: dict[tuple[int, int], list[tuple[int, int]]] = {}

    def add(a: tuple[int, int], b: tuple[int, int]) -> None:
        edges.setdefault(a, []).append(b)

    rows, cols = np.nonzero(padded)
    for r, c in zip(rows.tolist(), cols.tolist(), strict=True):
        if not padded[r - 1, c]:
            add((c, r), (c + 1, r))
        if not padded[r, c + 1]:
            add((c + 1, r), (c + 1, r + 1))
        if not padded[r + 1, c]:
            add((c + 1, r + 1), (c, r + 1))
        if not padded[r, c - 1]:
            add((c, r + 1), (c, r))

    loops: list[np.ndarray] = []
    while edges:
        start = next(iter(edges))
        loop = [start]
        node = start
        while True:
            outgoing = edges.get(node)
            if not outgoing:
                break
            nxt = outgoing.pop()
            if not outgoing:
                del edges[node]
            if nxt == start:
                break
            loop.append(nxt)
            node = nxt
        if len(loop) >= 4:
            loops.append(np.array(loop, dtype=float))
    return loops


def _smooth_ring(ring: np.ndarray, window: int) -> np.ndarray:
    """Low-pass the ring to remove the pixel staircase.

    The tracer walks real pixel edges, so every vertex lands on an integer and
    a diagonal comes out serrated. Simplification alone cannot fix that -- it
    picks a subset of the same quantised points. Averaging along the ring moves
    them off the lattice, which is what actually removes the stair.
    """
    n = len(ring)
    if window < 3 or n < window * 3:
        return ring
    kernel = np.ones(window) / window
    ext = np.vstack([ring[-window:], ring, ring[:window]])
    sx = np.convolve(ext[:, 0], kernel, mode="same")[window:-window]
    sy = np.convolve(ext[:, 1], kernel, mode="same")[window:-window]
    return np.column_stack([sx, sy])


def trace_polygons(mask: np.ndarray, opts: EmbossOptions) -> TraceResult:
    """Silhouette to simplified polygons with holes, in millimetres."""
    from shapely.geometry import Polygon
    from shapely.validation import make_valid

    mask, notes = _clean(mask, opts)
    loops = _boundary_loops(mask)
    if not loops:
        return TraceResult(notes=[*notes, "no contour could be traced"])

    window = max(3, round(2.5 * opts.smooth_px) | 1)
    rings = []
    for loop in loops:
        poly = Polygon(_smooth_ring(loop, window))
        if not poly.is_valid:
            poly = make_valid(poly)
        if poly.is_empty or poly.area <= 0:
            continue
        if opts.simplify_px > 0:
            poly = poly.simplify(opts.simplify_px, preserve_topology=True)
        if poly.geom_type == "MultiPolygon":
            poly = max(poly.geoms, key=lambda g: g.area)
        if poly.is_valid and poly.area > 0:
            rings.append(poly)

    rings.sort(key=lambda p: p.area, reverse=True)
    outers: list = []
    holes: list[list] = []
    for poly in rings:
        for i, outer in enumerate(outers):
            if outer.contains(poly.representative_point()):
                holes[i].append(poly)
                break
        else:
            outers.append(poly)
            holes.append([])

    built = []
    for outer, hs in zip(outers, holes, strict=True):
        shell = list(outer.exterior.coords)[:-1]
        rings_in = [list(h.exterior.coords)[:-1] for h in hs]
        built.append((shell, rings_in))

    # Map pixels to millimetres: y flips because image rows run downward.
    xs = [x for shell, hs in built for x, _ in shell] + [
        x for _, hs in built for h in hs for x, _ in h
    ]
    ys = [y for shell, hs in built for _, y in shell] + [
        y for _, hs in built for h in hs for _, y in h
    ]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    art_w = max(opts.width_mm - 2 * opts.margin_mm, 1.0)
    scale = art_w / max(x1 - x0, 1e-6)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2

    def to_mm(ring):
        return [
            (round((x - cx) * scale, 3), round(-(y - cy) * scale, 3)) for x, y in ring
        ]

    polygons = [(to_mm(shell), [to_mm(h) for h in hs]) for shell, hs in built]
    total = sum(len(s) + sum(len(h) for h in hs) for s, hs in polygons)
    if total > opts.max_points:
        notes.append(
            f"{total} contour points; raise --simplify to shrink the emitted script"
        )
    return TraceResult(
        polygons=polygons,
        notes=notes,
        point_count=total,
        holes=sum(len(hs) for _, hs in polygons),
    )


# ------------------------------------------------------------------- source


def _fmt(ring: list[tuple[float, float]]) -> str:
    parts = [f"({x}, {y})" for x, y in ring]
    lines, cur = [], "        "
    for part in parts:
        if len(cur) + len(part) > 88:
            lines.append(cur.rstrip())
            cur = "        "
        cur += part + ", "
    lines.append(cur.rstrip().rstrip(","))
    return "\n".join(lines)


def emboss_source(trace: TraceResult, opts: EmbossOptions, source_name: str) -> str:
    """Emit a standalone build123d script for the traced silhouette.

    The contour is data and is emitted as data. Everything that governs
    printability stays a named constant at the top, so the script edits and
    re-runs like any other bundle source.
    """
    # The panel is sized to the art it carries rather than fixed: a tall
    # silhouette on a landscape panel is the dead space the mushroom template
    # had to be retuned to avoid.
    ys = [y for shell, holes in trace.polygons for _, y in shell]
    art_h = (max(ys) - min(ys)) if ys else 1.0
    # The top margin has to clear the hanging hole, not just look like a
    # margin: at the default inset the hole otherwise lands inside the art.
    top_pad = (
        opts.margin_mm
        if opts.standalone
        else max(opts.margin_mm, opts.hang_inset_mm + opts.hang_d_mm / 2 + 3.0)
    )
    panel_h = art_h + top_pad + opts.margin_mm
    art_y = (opts.margin_mm - top_pad) / 2

    shapes = []
    for shell, holes in trace.polygons:
        hole_src = ",\n".join(f"        [\n{_fmt(h)}\n        ]" for h in holes)
        shapes.append(
            "    (\n        [\n"
            + _fmt(shell)
            + "\n        ],\n        ["
            + (("\n" + hole_src + "\n        ") if holes else "")
            + "],\n    ),"
        )
    body = "\n".join(shapes)

    panel_block = (
        ""
        if opts.standalone
        else """
    with BuildSketch() as plan:
        RectangleRounded(PANEL_W_MM, PANEL_H_MM, CORNER_R_MM)
    extrude(amount=PANEL_T_MM)

    with BuildSketch(Plane.XY) as hanger:
        with Locations((0, PANEL_H_MM / 2 - HANG_INSET_MM)):
            Circle(HANG_D_MM / 2)
    extrude(amount=PANEL_T_MM, mode=Mode.SUBTRACT)
"""
    )
    plane = "Plane.XY" if opts.standalone else "Plane.XY.offset(PANEL_T_MM)"
    thickness = "PANEL_T_MM + RELIEF_MM" if opts.standalone else "RELIEF_MM"

    return f'''"""Generated by FormForge from {source_name}.

The silhouette below was traced from the image and is data. Every constant
above it is parametric and owns the printability of the piece -- change one
and re-run this file:

    pip install build123d && python source.py
"""
from build123d import *

PANEL_W_MM = {opts.width_mm}
PANEL_H_MM = {panel_h:.2f}
PANEL_T_MM = {opts.panel_t_mm}
RELIEF_MM = {opts.relief_mm}
CORNER_R_MM = {opts.corner_r_mm}
HANG_D_MM = {opts.hang_d_mm}
HANG_INSET_MM = {opts.hang_inset_mm}
ART_Y_MM = {art_y:.2f}

# (outer ring, [hole rings]) in millimetres, centred on the origin.
SHAPES = [
{body}
]


def silhouette(shell, holes):
    """One traced shape as its own face, so a hole never reaches a neighbour."""
    with BuildSketch() as sketch:
        with BuildLine():
            Polyline(*shell, close=True)
        make_face()
        for ring in holes:
            with BuildLine():
                Polyline(*ring, close=True)
            make_face(mode=Mode.SUBTRACT)
    return sketch.sketch


with BuildPart() as art:{panel_block}
    with BuildSketch({plane}) as relief:
        for shell, holes in SHAPES:
            add(silhouette(shell, holes).moved(Location((0, ART_Y_MM))))
    extrude(amount={thickness})

result = art.part
'''
