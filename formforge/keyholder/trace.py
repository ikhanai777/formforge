"""Mask to closed polygons: marching squares, then Shapely.

The contour tracer is fifty lines of numpy rather than a dependency because the
requirement is narrow: one scalar field, one level, closed rings, sub-pixel
interpolation. Everything harder than that -- validity, containment, offsetting,
simplification -- is Shapely's job, and Shapely is already a dependency of the
render service.

The output is a polygon in *pixel* space with y pointing up. Scaling into
millimetres happens in `design`, because the manufacturing cleanup (removing
features thinner than a nozzle can print) is defined in millimetres and has to
be applied after the scale is known.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from shapely import affinity
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

# Contour level for a mask smoothed to a 0..1 field. 0.5 is the pixel edge.
LEVEL = 0.5

# Rings shorter than this many segments are sampling noise, not shapes.
MIN_RING_POINTS = 8


class TraceError(Exception):
    """The mask produced nothing that can be turned into a printable outline."""


@dataclass
class TraceStats:
    """What the tracer did, for the report."""

    rings: int = 0
    holes_found: int = 0
    holes_kept: int = 0
    points_before: int = 0
    points_after: int = 0
    simplify_px: float = 0.0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        payload = {
            "rings": self.rings,
            "holes_found": self.holes_found,
            "holes_kept": self.holes_kept,
            "points_before": self.points_before,
            "points_after": self.points_after,
            "simplify_px": round(self.simplify_px, 3),
        }
        if self.notes:
            payload["notes"] = list(self.notes)
        return payload


# ---------------------------------------------------------------------------
# Marching squares
# ---------------------------------------------------------------------------


def _interp(a: float, b: float, level: float) -> float:
    """Where between two corner values the level is crossed."""
    span = b - a
    if abs(span) < 1e-12:
        return 0.5
    return float(min(1.0, max(0.0, (level - a) / span)))


def marching_squares(field_: np.ndarray, level: float = LEVEL) -> list[np.ndarray]:
    """Closed contours of a scalar field, as arrays of (x, y) pixel coordinates.

    Only cells that straddle the level are visited, so the cost is proportional
    to the length of the boundary rather than the area of the image.

    Saddle cells (two opposite corners inside, two outside) are resolved by the
    average of the four corners, the standard disambiguation: it connects the
    contour the way the underlying field actually behaves rather than picking a
    fixed convention that produces a pinch in half the cases.
    """
    if field_.ndim != 2 or min(field_.shape) < 2:
        raise TraceError("the traced field is too small to contain a contour")

    inside = field_ >= level
    ul, ur = inside[:-1, :-1], inside[:-1, 1:]
    ll, lr = inside[1:, :-1], inside[1:, 1:]
    total = ul.astype(np.uint8) + ur + ll + lr
    mixed = (total > 0) & (total < 4)

    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for row, col in np.argwhere(mixed):
        r, c = int(row), int(col)
        v_ul = float(field_[r, c])
        v_ur = float(field_[r, c + 1])
        v_lr = float(field_[r + 1, c + 1])
        v_ll = float(field_[r + 1, c])

        top = (c + _interp(v_ul, v_ur, level), float(r))
        right = (float(c + 1), r + _interp(v_ur, v_lr, level))
        bottom = (c + _interp(v_ll, v_lr, level), float(r + 1))
        left = (float(c), r + _interp(v_ul, v_ll, level))

        case = (
            (8 if inside[r, c] else 0)
            + (4 if inside[r, c + 1] else 0)
            + (2 if inside[r + 1, c + 1] else 0)
            + (1 if inside[r + 1, c] else 0)
        )
        if case in (1, 14):
            segments.append((left, bottom))
        elif case in (2, 13):
            segments.append((bottom, right))
        elif case in (3, 12):
            segments.append((left, right))
        elif case in (4, 11):
            segments.append((top, right))
        elif case in (6, 9):
            segments.append((top, bottom))
        elif case in (7, 8):
            segments.append((left, top))
        elif case in (5, 10):
            centre = (v_ul + v_ur + v_lr + v_ll) / 4.0
            connected = (centre >= level) == (case == 5)
            if connected:
                segments.append((left, top))
                segments.append((bottom, right))
            else:
                segments.append((left, bottom))
                segments.append((top, right))

    return _chain(segments)


def _key(point: tuple[float, float]) -> tuple[int, int]:
    """Quantised endpoint identity.

    Endpoints shared between two cells are computed from the same pair of corner
    values, so they agree to the last bit; rounding to a millionth of a pixel is
    belt and braces against a compiler reassociating the arithmetic.
    """
    return (round(point[0] * 1e6), round(point[1] * 1e6))


def _chain(segments: list[tuple[tuple[float, float], tuple[float, float]]]) -> list[np.ndarray]:
    """Join undirected segments into closed rings."""
    if not segments:
        return []

    adjacency: dict[tuple[int, int], list[int]] = {}
    for index, (start, end) in enumerate(segments):
        adjacency.setdefault(_key(start), []).append(index)
        adjacency.setdefault(_key(end), []).append(index)

    used = [False] * len(segments)
    rings: list[np.ndarray] = []

    for seed in range(len(segments)):
        if used[seed]:
            continue
        used[seed] = True
        start, end = segments[seed]
        points = [start, end]
        while True:
            candidates = [i for i in adjacency.get(_key(points[-1]), []) if not used[i]]
            if not candidates:
                break
            index = candidates[0]
            used[index] = True
            a, b = segments[index]
            points.append(b if _key(a) == _key(points[-1]) else a)
            if _key(points[-1]) == _key(points[0]):
                break
        if len(points) >= MIN_RING_POINTS and _key(points[-1]) == _key(points[0]):
            rings.append(np.asarray(points[:-1], dtype=np.float64))

    return rings


# ---------------------------------------------------------------------------
# Rings to a polygon
# ---------------------------------------------------------------------------


def _ring_polygon(ring: np.ndarray) -> Polygon | None:
    try:
        polygon = Polygon(ring)
    except Exception:
        return None
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    if polygon.is_empty:
        return None
    if isinstance(polygon, MultiPolygon):
        polygon = max(polygon.geoms, key=lambda g: g.area)
    return polygon if polygon.area > 0 else None


def rings_to_polygon(
    rings: list[np.ndarray],
    *,
    min_hole_area_fraction: float = 0.002,
    stats: TraceStats | None = None,
) -> Polygon:
    """The outer ring with its direct children as holes.

    Rings nested two deep -- an island inside a hole, the dot inside a zero --
    are dropped rather than kept: a printable key holder is one solid, and an
    island is by definition not connected to it. Dropping it is reported.
    """
    polygons = [p for p in (_ring_polygon(r) for r in rings) if p is not None]
    if not polygons:
        raise TraceError(
            "no closed outline could be traced from the image. It may be all one "
            "colour, or the subject may fill the whole frame."
        )

    polygons.sort(key=lambda p: p.area, reverse=True)
    shell = polygons[0]
    if stats is not None:
        stats.rings = len(polygons)

    candidates = [p for p in polygons[1:] if shell.contains(p.representative_point())]
    holes: list[Polygon] = []
    islands = 0
    for candidate in candidates:
        depth = sum(
            1
            for other in candidates
            if other is not candidate and other.contains(candidate.representative_point())
        )
        if depth:
            islands += 1
        elif candidate.area >= shell.area * min_hole_area_fraction:
            holes.append(candidate)

    if stats is not None:
        stats.holes_found = len(candidates)
        stats.holes_kept = len(holes)
        dropped = len(candidates) - len(holes) - islands
        if dropped:
            stats.notes.append(
                f"{dropped} opening(s) smaller than "
                f"{min_hole_area_fraction:.1%} of the outline were ignored"
            )
        if islands:
            stats.notes.append(
                f"{islands} detached island(s) inside an opening were dropped: a "
                "key holder has to be one connected piece"
            )

    result = Polygon(
        shell.exterior.coords,
        [hole.exterior.coords for hole in holes],
    )
    if not result.is_valid:
        result = result.buffer(0)
        if isinstance(result, MultiPolygon):
            result = max(result.geoms, key=lambda g: g.area)
    return orient(result, sign=1.0)


def trace_polygon(
    field_: np.ndarray,
    *,
    simplify_px: float = 0.6,
    min_hole_area_fraction: float = 0.002,
    level: float = LEVEL,
) -> tuple[Polygon, TraceStats]:
    """Full trace: field -> simplified polygon in pixel space, y pointing up.

    Simplification happens in pixel space and before scaling because its natural
    unit is the sampling grid: a tolerance under one pixel removes vertices that
    only encode where the pixel edges were, and keeps every vertex that encodes
    the shape.
    """
    stats = TraceStats(simplify_px=simplify_px)
    rings = marching_squares(field_, level=level)
    stats.points_before = int(sum(len(r) for r in rings))
    polygon = rings_to_polygon(
        rings, min_hole_area_fraction=min_hole_area_fraction, stats=stats
    )

    if simplify_px > 0:
        simplified = polygon.simplify(simplify_px, preserve_topology=True)
        if simplified.is_valid and not simplified.is_empty and simplified.area > 0:
            polygon = simplified if isinstance(simplified, Polygon) else polygon

    # Flip to a y-up frame so the model's Y axis is the wall's up. Rows count
    # downwards in an image and upwards in every CAD kernel; doing it once,
    # here, is what stops a mirrored key holder.
    height = float(field_.shape[0])
    polygon = affinity.scale(polygon, xfact=1.0, yfact=-1.0, origin=(0.0, 0.0))
    polygon = affinity.translate(polygon, yoff=height)

    polygon = orient(_single(polygon), sign=1.0)
    stats.points_after = _count_points(polygon)
    return polygon, stats


def _single(geometry) -> Polygon:
    """Reduce a geometry to its largest polygon."""
    if isinstance(geometry, Polygon):
        return geometry
    if isinstance(geometry, MultiPolygon):
        if geometry.is_empty:
            raise TraceError("the outline collapsed to nothing")
        return max(geometry.geoms, key=lambda g: g.area)
    merged = unary_union(geometry)
    if isinstance(merged, (Polygon, MultiPolygon)):
        return _single(merged)
    raise TraceError("the traced outline is not an area")


def _count_points(polygon: Polygon) -> int:
    return len(polygon.exterior.coords) + sum(
        len(interior.coords) for interior in polygon.interiors
    )
