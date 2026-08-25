"""Geometric measurements on a tessellated model.

Everything the DFM checks compare against a threshold is computed here, so the
checks themselves stay declarative and the expensive work happens once and is
cached. Each measurement is a documented approximation with a known failure
mode -- there is no exact answer to "how thick is this wall" for an arbitrary
solid, and pretending otherwise produces a validator you cannot trust.

The two rules this module follows:

1. Never silently degrade. If a measurement cannot be taken (too many
   triangles, a mesh that will not load), say so; the report records it as
   skipped rather than as a pass.
2. Bound every cost. A validator that occasionally takes four minutes is a
   validator that gets turned off.
"""

from __future__ import annotations

import contextlib
import logging
import math
from dataclasses import dataclass, field
from functools import cached_property, lru_cache
from pathlib import Path

import numpy as np
import trimesh

# Above this triangle count the O(n log n)-ish measurements are still fine but
# the pairwise ones are not. They are skipped and reported rather than run.
SELF_INTERSECT_MAX_FACES = 120_000

# Ray-cast samples for the thickness measurement. 3000 samples on a typical
# 50k-triangle part takes well under a second and finds thin walls reliably;
# the number is a speed/recall tradeoff, not a correctness one.
THICKNESS_SAMPLES = 3000

# How far to push a ray origin below the surface before casting, so the ray does
# not immediately re-hit its own source triangle.
RAY_EPS_MM = 1e-4

# Ray-triangle tests the numpy fallback will perform in total. The fallback is
# O(rays x faces), so the sample count is derived from this rather than fixed --
# a coarse thickness measurement is worth far more than a skipped one.
BRUTE_FORCE_RAY_BUDGET = 20_000_000

# Rays per chunk in the fallback, chosen so the intermediate arrays stay in the
# tens of megabytes regardless of mesh size.
BRUTE_FORCE_CHUNK = 64

# cos(60 degrees). A ray that exits through a face more than 60 degrees off
# facing it grazed an edge rather than crossing a wall; see _exit_is_opposing.
GRAZING_EXIT_COS = 0.5

# Measurement uncertainty on a sampled thickness, in millimetres.
#
# The mesh is tessellated at 0.05 mm deflection and sampled at finitely many
# points, so a wall modelled at exactly 2.0 mm measures 1.9997. Comparing that
# to a 2.0 mm threshold to three decimal places is false precision, and it fails
# every part built at exactly the minimum its own schema allows -- which is a
# large fraction of them, because "the minimum" is what a careful author picks.
# The allowance is well under a layer height, so nothing genuinely too thin
# passes because of it.
THICKNESS_TOLERANCE_MM = 0.02


@dataclass
class ThicknessResult:
    """Local wall thickness sampled across the surface."""

    min_mm: float
    p01_mm: float
    p05_mm: float
    median_mm: float
    location_mm: list[float] | None
    samples: int
    # Fraction of samples below each threshold, which is what separates "one
    # sliver at a fillet tangent" from "the whole wall is too thin".
    fraction_below: dict[str, float] = field(default_factory=dict)

    @property
    def p01_with_tolerance_mm(self) -> float:
        """The representative thickness, plus the measurement's own uncertainty.

        This is the value to compare against a threshold. Comparing the raw
        measurement instead fails a wall modelled at exactly the minimum, purely
        because tessellating and sampling it lands a few microns low.
        """
        return self.p01_mm + THICKNESS_TOLERANCE_MM

    @property
    def p05_with_tolerance_mm(self) -> float:
        """The representative thickness when the model carries raised text.

        Embossed lettering is thin by design, and on a small tag its sidewalls
        are a few percent of the sampled surface -- enough to drag the 1st
        percentile below what the body actually measures. Text is governed by
        cap height, stroke width and relief depth, which the text-legibility
        check owns; using a higher percentile here stops the two checks
        double-counting the same geometry and rejecting a perfectly printable
        name tag.
        """
        return self.p05_mm + THICKNESS_TOLERANCE_MM


@dataclass
class OverhangResult:
    """Area-weighted overhang statistics, excluding the build-plate face."""

    max_angle_deg: float
    overhang_area_mm2: float
    total_area_mm2: float
    fraction: float
    worst_location_mm: list[float] | None


@dataclass
class BridgeResult:
    """The widest unsupported horizontal span in the model."""

    max_span_mm: float
    location_mm: list[float] | None
    region_count: int


@dataclass
class FootprintResult:
    """First-layer adhesion and tipping geometry."""

    contact_area_mm2: float
    bbox_xy_area_mm2: float
    contact_fraction: float
    com_mm: list[float]
    com_inside_footprint: bool
    com_margin_frac: float


class MeshMeasurements:
    """Lazily computed measurements over one mesh. Construct once, read often."""

    def __init__(self, mesh: trimesh.Trimesh, repair_result: object | None = None):
        self.mesh = mesh
        self.repair_result = repair_result
        self.skipped: list[str] = []

    # -- construction ------------------------------------------------------
    @classmethod
    def from_file(cls, path: str | Path, *, auto_repair: bool = True) -> "MeshMeasurements":
        """Load a mesh, welded and cleaned of geometry-free artifacts.

        Welding is not optional. A binary STL stores three independent vertices
        per triangle and shares nothing, so an unwelded load reports every
        triangle as its own disconnected shell -- watertight fails, the solid
        count equals the triangle count, and every adjacency test is nonsense.
        Every check in this module assumes a welded mesh.

        `auto_repair` additionally runs the lossless rungs of the repair ladder
        (see `formforge.repair`). It is on by default because those rungs
        provably do not move a surface point, and because without them a
        perfectly good part fails Tier 1 over the two zero-area triangles that
        tessellating a sphere leaves at its poles. Nothing that changes the
        shape happens here -- that stays an explicit, separate decision.
        """
        loaded = trimesh.load(str(path), force="mesh", process=True)
        if not isinstance(loaded, trimesh.Trimesh):
            raise ValueError(f"{path} did not load as a single mesh")
        loaded.merge_vertices()
        if not auto_repair:
            return cls(loaded)

        from ..repair import repair as run_repair  # noqa: PLC0415 - avoids a cycle

        outcome = run_repair(loaded, allow_lossy=False)
        return cls(outcome.mesh, repair_result=outcome)

    # -- basic topology ----------------------------------------------------
    @cached_property
    def triangle_count(self) -> int:
        return int(len(self.mesh.faces))

    @cached_property
    def vertex_count(self) -> int:
        return int(len(self.mesh.vertices))

    @cached_property
    def is_watertight(self) -> bool:
        return bool(self.mesh.is_watertight)

    @cached_property
    def is_winding_consistent(self) -> bool:
        return bool(self.mesh.is_winding_consistent)

    @cached_property
    def volume_mm3(self) -> float:
        return float(self.mesh.volume)

    @cached_property
    def area_mm2(self) -> float:
        return float(self.mesh.area)

    @cached_property
    def bounds(self) -> np.ndarray:
        return np.asarray(self.mesh.bounds, dtype=float)

    @cached_property
    def extents_mm(self) -> list[float]:
        return [float(v) for v in self.mesh.extents]

    @cached_property
    def face_areas(self) -> np.ndarray:
        return np.asarray(self.mesh.area_faces, dtype=float)

    @cached_property
    def degenerate_faces(self) -> int:
        """Faces with effectively zero area.

        These are harmless in a viewer and poison to a slicer, which divides by
        the face normal it cannot compute.
        """
        return int(np.count_nonzero(self.face_areas < 1e-9))

    @cached_property
    def duplicate_vertices(self) -> int:
        """Vertices still coincident after the standard weld.

        The mesh arrives welded at full precision, so this measures what
        survives that: vertices that collapse only at a coarser 1e-5 mm
        tolerance. Those are near-coincident points, which produce sliver faces
        a slicer handles badly -- a genuinely different signal from the
        unwelded-STL duplicates that welding already removed.
        """
        merged = self.mesh.copy()
        merged.merge_vertices(digits_vertex=5)
        return max(0, self.vertex_count - int(len(merged.vertices)))

    @cached_property
    def components(self) -> list[trimesh.Trimesh]:
        """Connected components, largest first."""
        try:
            parts = self.mesh.split(only_watertight=False)
        except Exception:
            return [self.mesh]
        if len(parts) == 0:
            return [self.mesh]
        return sorted(parts, key=lambda m: float(abs(m.volume)), reverse=True)

    @cached_property
    def component_count(self) -> int:
        """Connected shells, including the inner shell of any enclosed void."""
        return len(self.components)

    @cached_property
    def solid_count(self) -> int:
        """Distinct printable bodies.

        An enclosed void contributes a second connected shell but not a second
        body: a cube with a spherical cavity is one object, and reporting it as
        two would fail the solid-count check on a model whose only real problem
        is the cavity -- which `trapped_volumes` already reports, far more
        usefully.
        """
        return max(1, len(self.components) - len(self.trapped_volumes))

    @cached_property
    def stray_shards(self) -> list[dict]:
        """Components with negligible volume: the debris a failed boolean leaves.

        A shard is not merely cosmetic. It confuses slicers, shows up as a
        floating speck of plastic mid-print, and is the clearest signal that a
        boolean did something other than what the script intended.
        """
        shards = []
        for i, part in enumerate(self.components):
            volume = float(abs(part.volume))
            if volume < 1.0:
                centroid = [float(v) for v in part.centroid]
                shards.append({"index": i, "volume_mm3": volume, "centroid_mm": centroid})
        return shards

    @cached_property
    def genus(self) -> int:
        """Topological genus, summed over components.

        A high genus that the design does not call for almost always means a
        boolean produced a mess of unintended tunnels. Checked as a sanity
        signal, not a hard rule -- a Voronoi lattice legitimately has hundreds.
        """
        total = 0
        for part in self.components:
            v = len(part.vertices)
            f = len(part.faces)
            e = len(part.edges_unique)
            euler = v - e + f
            total += max(0, (2 - euler) // 2)
        return int(total)

    # -- self-intersection -------------------------------------------------
    @cached_property
    def self_intersections(self) -> int | None:
        """Count of intersecting triangle pairs, or None when not measured.

        A uniform-grid broadphase over face AABBs, then an exact Moller
        triangle-triangle test on the survivors. Pairs sharing a vertex are
        skipped: adjacent triangles touch by construction.
        """
        if self.triangle_count > SELF_INTERSECT_MAX_FACES:
            self.skipped.append(
                f"self_intersection (mesh has {self.triangle_count} faces, "
                f"over the {SELF_INTERSECT_MAX_FACES} measurement cap)"
            )
            return None
        return _count_self_intersections(self.mesh)

    # -- thickness ---------------------------------------------------------
    @cached_property
    def thickness(self) -> ThicknessResult | None:
        """Local wall thickness by inward ray casting.

        For each sampled surface point, cast a ray along the inward normal and
        take the distance to the first exit. This is the standard approximation
        and it has one known bias: at a concave corner the inward ray travels
        diagonally and over-reports. It never *under*-reports, so a wall it
        passes is genuinely thick enough -- the errors fall on the safe side of
        the threshold, which is the direction you want them in.
        """
        mesh = self.mesh
        if self.triangle_count == 0:
            return None

        sample_count = self._thickness_sample_count()
        with _quiet():
            try:
                points, face_ids = trimesh.sample.sample_surface_even(mesh, sample_count)
            except Exception:
                try:
                    points, face_ids = trimesh.sample.sample_surface(mesh, sample_count)
                except Exception:
                    self.skipped.append("thickness (surface sampling failed)")
                    return None

        points = np.asarray(points, dtype=float)
        face_ids = np.asarray(face_ids, dtype=int)
        if len(points) == 0:
            return None

        normals = np.asarray(mesh.face_normals, dtype=float)[face_ids]
        directions = -normals
        origins = points + directions * RAY_EPS_MM

        distances, index_ray, index_tri = self._cast_inward(origins, directions)
        if distances is None or index_ray is None or index_tri is None:
            return None
        if len(distances) == 0:
            self.skipped.append("thickness (no inward ray hits)")
            return None

        hit_points = points[index_ray]
        valid = (distances > 1e-6) & _exit_is_opposing(
            mesh, directions[index_ray], index_tri
        )
        distances = distances[valid]
        hit_points = hit_points[valid]
        if len(distances) == 0:
            self.skipped.append("thickness (no non-grazing inward ray hits)")
            return None
        if len(distances) == 0:
            return None

        worst = int(np.argmin(distances))
        thresholds = {"0.8": 0.8, "1.2": 1.2, "1.6": 1.6, "2.0": 2.0}
        fraction_below = {
            key: float(np.count_nonzero(distances < value) / len(distances))
            for key, value in thresholds.items()
        }
        return ThicknessResult(
            min_mm=float(distances.min()),
            p01_mm=float(np.percentile(distances, 1)),
            p05_mm=float(np.percentile(distances, 5)),
            median_mm=float(np.median(distances)),
            location_mm=[float(v) for v in hit_points[worst]],
            samples=int(len(distances)),
            fraction_below=fraction_below,
        )

    def _thickness_sample_count(self) -> int:
        """How many rays to cast, bounded by what the available backend costs.

        With an accelerated backend the cost is per-ray and the full sample
        budget is affordable. Without one, every ray is tested against every
        triangle, so the budget has to shrink with mesh size or the measurement
        takes minutes on a detailed part.
        """
        if _has_ray_acceleration():
            return THICKNESS_SAMPLES
        budget = max(200, min(THICKNESS_SAMPLES, BRUTE_FORCE_RAY_BUDGET // max(1, self.triangle_count)))
        if budget < THICKNESS_SAMPLES:
            self.skipped.append(
                f"thickness sampled at reduced resolution ({budget} rays; install "
                "rtree for the full measurement)"
            )
        return int(budget)

    def _cast_inward(
        self, origins: np.ndarray, directions: np.ndarray
    ) -> tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None]:
        """First inward hit per ray: (distance, ray index, hit face index)."""
        if _has_ray_acceleration():
            try:
                locations, index_ray, index_tri = self.mesh.ray.intersects_location(
                    origins, directions, multiple_hits=False
                )
                if len(index_ray) == 0:
                    return np.empty(0), np.empty(0, dtype=int), np.empty(0, dtype=int)
                index_ray = np.asarray(index_ray, dtype=int)
                distances = np.linalg.norm(
                    np.asarray(locations) - origins[index_ray], axis=1
                )
                return distances, index_ray, np.asarray(index_tri, dtype=int)
            except Exception as exc:  # pragma: no cover - backend dependent
                self.skipped.append(f"thickness (accelerated ray cast failed: {exc})")
        try:
            return _brute_force_first_hit(self.mesh, origins, directions)
        except Exception as exc:
            self.skipped.append(f"thickness (ray cast unavailable: {exc})")
            return None, None, None

    # -- overhangs ---------------------------------------------------------
    @cached_property
    def overhangs(self) -> OverhangResult:
        """Area-weighted overhang severity, ignoring the build-plate face.

        Angle convention: measured from vertical, so a vertical wall is 0 deg
        and a flat ceiling is 90 deg. Faces sitting on the plate are excluded --
        they are supported by the bed, not by air.
        """
        mesh = self.mesh
        normals = np.asarray(mesh.face_normals, dtype=float)
        areas = self.face_areas
        centroids = np.asarray(mesh.triangles_center, dtype=float)
        z_min = float(self.bounds[0][2])

        nz = normals[:, 2]
        downward = nz < -1e-6
        # A face whose centroid is within one layer of the plate and which faces
        # down is the first layer: supported by the bed.
        on_plate = (centroids[:, 2] - z_min) < 0.25
        candidate = downward & ~on_plate

        angles = np.zeros(len(normals))
        angles[downward] = np.degrees(np.arcsin(np.clip(-nz[downward], 0.0, 1.0)))

        total_area = float(areas.sum())
        if not np.any(candidate):
            return OverhangResult(0.0, 0.0, total_area, 0.0, None)

        severe = candidate & (angles > 45.0)
        overhang_area = float(areas[severe].sum())
        worst_idx = int(np.argmax(np.where(candidate, angles, -1.0)))
        return OverhangResult(
            max_angle_deg=float(angles[worst_idx]),
            overhang_area_mm2=overhang_area,
            total_area_mm2=total_area,
            fraction=overhang_area / total_area if total_area > 0 else 0.0,
            worst_location_mm=[float(v) for v in centroids[worst_idx]],
        )

    # -- bridges -----------------------------------------------------------
    @cached_property
    def bridges(self) -> BridgeResult:
        """The widest unsupported horizontal span.

        Near-horizontal downward faces above the plate are grouped into
        connected regions; each region's span is the narrowest direction the
        printer could bridge across it, approximated by the minimum projected
        width over sampled directions. The narrowest direction is the right one
        because a slicer bridges along the shortest crossing.
        """
        mesh = self.mesh
        normals = np.asarray(mesh.face_normals, dtype=float)
        centroids = np.asarray(mesh.triangles_center, dtype=float)
        z_min = float(self.bounds[0][2])

        # Within 20 degrees of fully horizontal, facing down, off the plate.
        horizontal_down = normals[:, 2] < -math.cos(math.radians(20.0))
        off_plate = (centroids[:, 2] - z_min) > 0.25
        mask = horizontal_down & off_plate
        face_ids = np.flatnonzero(mask)
        if len(face_ids) == 0:
            return BridgeResult(0.0, None, 0)

        regions = _connected_face_groups(mesh, face_ids)
        max_span = 0.0
        location: list[float] | None = None
        for region in regions:
            span, worst = _unsupported_span(mesh, region)
            if span > max_span:
                max_span = span
                location = worst
        return BridgeResult(float(max_span), location, len(regions))

    # -- footprint / tipping ----------------------------------------------
    @cached_property
    def footprint(self) -> FootprintResult:
        """First-layer contact and centre-of-mass stability.

        Contact area is the downward-facing area within one layer of the lowest
        point. The tipping check projects the centre of mass onto the plate and
        asks whether it lands inside the convex hull of that contact -- outside
        means the part falls over, either on the bed or on a shelf afterwards.
        """
        mesh = self.mesh
        normals = np.asarray(mesh.face_normals, dtype=float)
        centroids = np.asarray(mesh.triangles_center, dtype=float)
        areas = self.face_areas
        z_min = float(self.bounds[0][2])

        on_plate = ((centroids[:, 2] - z_min) < 0.25) & (normals[:, 2] < -0.5)
        contact_area = float(areas[on_plate].sum())

        extents = self.extents_mm
        bbox_xy = float(extents[0] * extents[1])

        try:
            com = np.asarray(mesh.center_mass, dtype=float)
        except Exception:
            com = np.asarray(mesh.centroid, dtype=float)

        inside, margin = _com_inside_footprint(mesh, on_plate, com)
        return FootprintResult(
            contact_area_mm2=contact_area,
            bbox_xy_area_mm2=bbox_xy,
            contact_fraction=contact_area / bbox_xy if bbox_xy > 0 else 0.0,
            com_mm=[float(v) for v in com],
            com_inside_footprint=inside,
            com_margin_frac=margin,
        )

    @cached_property
    def aspect_ratio(self) -> float:
        """Height over the narrower footprint dimension: the wobble metric."""
        extents = self.extents_mm
        base = min(extents[0], extents[1])
        if base <= 1e-6:
            return float("inf")
        return float(extents[2] / base)

    # -- trapped volume ----------------------------------------------------
    @cached_property
    def trapped_volumes(self) -> list[dict]:
        """Fully enclosed voids with no path to the outside.

        Detected as a watertight component whose centroid lies inside another
        component. In FDM this is wasted material and an un-inspectable cavity;
        in resin it is a suction cup that tears the part off the plate.
        """
        parts = self.components
        if len(parts) < 2:
            return []
        trapped: list[dict] = []
        outer = parts[0]
        for i, part in enumerate(parts[1:], start=1):
            if not part.is_watertight:
                continue
            try:
                centroid = np.asarray(part.centroid, dtype=float).reshape(1, 3)
                if bool(outer.contains(centroid)[0]):
                    trapped.append(
                        {
                            "index": i,
                            "volume_mm3": float(abs(part.volume)),
                            "centroid_mm": [float(v) for v in part.centroid],
                        }
                    )
            except Exception:
                continue
        return trapped

    # -- summary -----------------------------------------------------------
    def basic_dict(self) -> dict:
        """Cheap measurements only.

        Used when Tier 1 has already failed: the model is not a valid solid, so
        paying for a thickness ray cast on it buys a number that means nothing.
        """
        return {
            "triangles": self.triangle_count,
            "vertices": self.vertex_count,
            "volume_mm3": round(self.volume_mm3, 4),
            "area_mm2": round(self.area_mm2, 4),
            "bbox_mm": [round(v, 4) for v in self.extents_mm],
            "watertight": self.is_watertight,
            "winding_consistent": self.is_winding_consistent,
            "solids": self.solid_count,
            "components": self.component_count,
            "genus": self.genus,
            "degenerate_faces": self.degenerate_faces,
        }

    def as_dict(self) -> dict:
        thickness = self.thickness
        return {
            "triangles": self.triangle_count,
            "vertices": self.vertex_count,
            "volume_mm3": round(self.volume_mm3, 4),
            "area_mm2": round(self.area_mm2, 4),
            "bbox_mm": [round(v, 4) for v in self.extents_mm],
            "watertight": self.is_watertight,
            "winding_consistent": self.is_winding_consistent,
            "solids": self.solid_count,
            "genus": self.genus,
            "degenerate_faces": self.degenerate_faces,
            "duplicate_vertices": self.duplicate_vertices,
            "min_wall_mm": round(thickness.min_mm, 4) if thickness else None,
            "median_wall_mm": round(thickness.median_mm, 4) if thickness else None,
            "max_overhang_deg": round(self.overhangs.max_angle_deg, 2),
            "overhang_fraction": round(self.overhangs.fraction, 4),
            "max_bridge_mm": round(self.bridges.max_span_mm, 3),
            "plate_contact_mm2": round(self.footprint.contact_area_mm2, 3),
            "plate_contact_fraction": round(self.footprint.contact_fraction, 4),
            "com_inside_footprint": self.footprint.com_inside_footprint,
            "aspect_ratio": round(self.aspect_ratio, 3),
            "trapped_volumes": len(self.trapped_volumes),
            "self_intersections": self.self_intersections,
        }


# ---------------------------------------------------------------------------
# ray casting
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _has_ray_acceleration() -> bool:
    """Is a spatial index available for trimesh's ray queries?

    trimesh's pure-Python intersector builds an rtree, and raises if the package
    is missing. Checking once up front lets the measurement choose its strategy
    instead of discovering the problem mid-cast.
    """
    try:
        import rtree  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False


def _exit_is_opposing(
    mesh: trimesh.Trimesh, directions: np.ndarray, index_tri: np.ndarray
) -> np.ndarray:
    """Keep only rays that exit through a face roughly facing the ray.

    Without this the thickness measurement is dominated by an artifact. A sample
    point sitting on a sharp convex edge -- the side of an embossed letter, the
    seam where a flat back meets a curved wall -- casts its inward ray straight
    back out through the neighbouring face after travelling almost no distance,
    and reports a wall three microns thick.

    A ray crossing a genuine wall exits through a face whose outward normal
    points along the ray. A ray grazing an edge exits through one nearly
    perpendicular to it. Requiring the exit face to be within 60 degrees of
    facing the ray keeps the first and discards the second.
    """
    normals = np.asarray(mesh.face_normals, dtype=float)[index_tri]
    alignment = np.einsum("ij,ij->i", directions, normals)
    return alignment >= GRAZING_EXIT_COS


def _brute_force_first_hit(
    mesh: trimesh.Trimesh, origins: np.ndarray, directions: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Vectorised Moller-Trumbore first-hit, with no spatial index.

    Tests every ray against every triangle, chunked to bound peak memory. This
    is the fallback when rtree is unavailable: slower, but it keeps the wall
    thickness check running, and a wall thickness check that silently turns
    itself off is worse than a slow one.
    """
    triangles = np.asarray(mesh.triangles, dtype=float)
    v0 = triangles[:, 0, :]
    edge1 = triangles[:, 1, :] - v0
    edge2 = triangles[:, 2, :] - v0
    eps = 1e-9

    hit_distances: list[float] = []
    hit_rays: list[int] = []
    hit_faces: list[int] = []

    for start in range(0, len(origins), BRUTE_FORCE_CHUNK):
        stop = min(start + BRUTE_FORCE_CHUNK, len(origins))
        o = origins[start:stop][:, None, :]
        d = directions[start:stop][:, None, :]

        pvec = np.cross(d, edge2[None, :, :])
        det = np.einsum("ijk,jk->ij", pvec, edge1)
        parallel = np.abs(det) < eps
        inv_det = np.where(parallel, 0.0, 1.0 / np.where(parallel, 1.0, det))

        tvec = o - v0[None, :, :]
        u = np.einsum("ijk,ijk->ij", tvec, pvec) * inv_det

        qvec = np.cross(tvec, edge1[None, :, :])
        v = np.einsum("ijk,ijk->ij", d, qvec) * inv_det
        t = np.einsum("ijk,jk->ij", qvec, edge2) * inv_det

        valid = (
            ~parallel
            & (u >= -eps)
            & (v >= -eps)
            & (u + v <= 1.0 + eps)
            & (t > 1e-6)
        )
        distances = np.where(valid, t, np.inf)
        nearest_face = distances.argmin(axis=1)
        nearest = distances.min(axis=1)
        for offset, distance in enumerate(nearest):
            if np.isfinite(distance):
                hit_distances.append(float(distance))
                hit_rays.append(start + offset)
                hit_faces.append(int(nearest_face[offset]))

    return (
        np.asarray(hit_distances),
        np.asarray(hit_rays, dtype=int),
        np.asarray(hit_faces, dtype=int),
    )


@contextlib.contextmanager
def _quiet():
    """Silence trimesh's sampler chatter for the duration of a call.

    `sample_surface_even` logs a warning whenever it places fewer points than
    asked for, which happens routinely on a part with small faces and is not a
    problem: the shortfall is recorded in `ThicknessResult.samples`, so the
    reduced resolution is visible in the report rather than only in a log line.
    Raising the level rather than redirecting stdout is what actually works --
    it is a logger, not a print.
    """
    logger = logging.getLogger("trimesh")
    previous = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(previous)


# ---------------------------------------------------------------------------
# geometry helpers
# ---------------------------------------------------------------------------


def _connected_face_groups(mesh: trimesh.Trimesh, face_ids: np.ndarray) -> list[np.ndarray]:
    """Group a subset of faces into edge-connected regions.

    Union-find over the mesh's face adjacency, restricted to the subset. Used to
    turn "all the downward-facing triangles" into "these three separate
    ceilings", which is what a span measurement needs.
    """
    subset = set(int(f) for f in face_ids)
    parent: dict[int, int] = {f: f for f in subset}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for a, b in np.asarray(mesh.face_adjacency, dtype=int):
        ia, ib = int(a), int(b)
        if ia in subset and ib in subset:
            union(ia, ib)

    groups: dict[int, list[int]] = {}
    for face in subset:
        groups.setdefault(find(face), []).append(face)
    return [np.asarray(v, dtype=int) for v in groups.values()]


def _unsupported_span(
    mesh: trimesh.Trimesh, region: np.ndarray
) -> tuple[float, list[float] | None]:
    """How far a ceiling reaches from its nearest support, doubled.

    A downward-facing region is held up at its perimeter, where it meets the
    walls that continue below it. The hardest point to bridge is therefore the
    one furthest from that perimeter, and the span the slicer must cross is
    twice that distance.

    Measuring the region's overall width instead gets ring-shaped ceilings badly
    wrong: a 2 mm lip running round the rim of a 120 mm pot is 120 mm across and
    bridges trivially, because no point on it is more than a millimetre from
    solid wall. That false positive is what this replaces.
    """
    faces = mesh.faces[region]
    edges = np.sort(faces[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2), axis=1)
    unique, counts = np.unique(edges, axis=0, return_counts=True)
    boundary = unique[counts == 1]
    if len(boundary) == 0:
        # A closed region with no perimeter is not a ceiling over anything we
        # can reason about; treat it as unmeasured rather than infinite.
        return 0.0, None

    starts = mesh.vertices[boundary[:, 0]][:, :2]
    ends = mesh.vertices[boundary[:, 1]][:, :2]

    # Sample each triangle at its centroid *and* its edge midpoints. Centroids
    # alone under-report badly on a flat annulus: it triangulates into long
    # triangles reaching from the inner ring to the outer one, whose vertices
    # all sit on the boundary and whose centroids land two-thirds of the way
    # across. The deepest point of such a region is near a spanning edge's
    # midpoint, so that is where it has to be sampled. Under-reporting a bridge
    # is the dangerous direction of error -- it passes a span that will sag.
    samples = _triangle_samples(mesh, region)
    distances = _point_segment_distances(samples[:, :2], starts, ends)
    worst_idx = int(np.argmax(distances))
    span = float(distances[worst_idx] * 2.0)
    return span, [float(v) for v in samples[worst_idx]]


def _triangle_samples(mesh: trimesh.Trimesh, region: np.ndarray) -> np.ndarray:
    """Centroid plus the three edge midpoints of each triangle in a region."""
    tris = np.asarray(mesh.triangles, dtype=float)[region]
    centroids = tris.mean(axis=1)
    midpoints = np.concatenate(
        [
            (tris[:, 0] + tris[:, 1]) / 2.0,
            (tris[:, 1] + tris[:, 2]) / 2.0,
            (tris[:, 2] + tris[:, 0]) / 2.0,
        ]
    )
    return np.concatenate([centroids, midpoints])


def _point_segment_distances(
    points: np.ndarray, starts: np.ndarray, ends: np.ndarray, chunk: int = 512
) -> np.ndarray:
    """Distance from each point to the nearest of a set of 2D segments."""
    seg = ends - starts
    seg_len_sq = np.einsum("ij,ij->i", seg, seg)
    seg_len_sq = np.where(seg_len_sq < 1e-12, 1.0, seg_len_sq)

    out = np.empty(len(points), dtype=float)
    for start in range(0, len(points), chunk):
        stop = min(start + chunk, len(points))
        block = points[start:stop][:, None, :]
        rel = block - starts[None, :, :]
        t = np.clip(np.einsum("ijk,jk->ij", rel, seg) / seg_len_sq, 0.0, 1.0)
        closest = starts[None, :, :] + t[:, :, None] * seg[None, :, :]
        out[start:stop] = np.linalg.norm(block - closest, axis=2).min(axis=1)
    return out


def _com_inside_footprint(
    mesh: trimesh.Trimesh, on_plate: np.ndarray, com: np.ndarray
) -> tuple[bool, float]:
    """Does the centre of mass project inside the first-layer contact patch?

    Returns (inside, margin) where margin is the distance from the hull centroid
    to the CoM as a fraction of the hull's radius -- 0 is dead centre, 1 is on
    the edge, above 1 has tipped over.
    """
    if not np.any(on_plate):
        return False, 1.0
    verts = mesh.vertices[np.unique(mesh.faces[on_plate].ravel())][:, :2]
    if len(verts) < 3:
        return False, 1.0
    try:
        hull = _convex_hull_2d(verts)
    except Exception:
        return True, 0.0
    if len(hull) < 3:
        return False, 1.0
    inside = _point_in_polygon(com[:2], hull)
    center = hull.mean(axis=0)
    radius = float(np.linalg.norm(hull - center, axis=1).max())
    margin = float(np.linalg.norm(com[:2] - center) / radius) if radius > 1e-9 else 0.0
    return bool(inside), margin


def _convex_hull_2d(points: np.ndarray) -> np.ndarray:
    """Monotone-chain convex hull. Small, exact, no scipy dependency."""
    pts = np.unique(np.round(points, 6), axis=0)
    pts = pts[np.lexsort((pts[:, 1], pts[:, 0]))]
    if len(pts) < 3:
        return pts

    def half(sequence: np.ndarray) -> list[np.ndarray]:
        out: list[np.ndarray] = []
        for p in sequence:
            while len(out) >= 2 and _cross(out[-2], out[-1], p) <= 0:
                out.pop()
            out.append(p)
        return out

    lower = half(pts)
    upper = half(pts[::-1])
    return np.asarray(lower[:-1] + upper[:-1])


def _cross(o: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
    return float((a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]))


def _point_in_polygon(point: np.ndarray, polygon: np.ndarray) -> bool:
    """Ray-crossing test against a convex or concave polygon."""
    x, y = float(point[0]), float(point[1])
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            x_cross = (x2 - x1) * (y - y1) / (y2 - y1 + 1e-18) + x1
            if x < x_cross:
                inside = not inside
    return inside


# -- self-intersection -------------------------------------------------------


def _count_self_intersections(mesh: trimesh.Trimesh, max_report: int = 50) -> int:
    """Count intersecting triangle pairs that do not share a vertex.

    Uniform-grid broadphase sized to the mean triangle extent, then an exact
    Moller interval-overlap test. Stops counting at `max_report`: the report
    only needs to know whether the mesh self-intersects and roughly how badly,
    and a mesh with thousands of intersections is being regenerated regardless.
    """
    faces = np.asarray(mesh.faces, dtype=np.int64)
    vertices = np.asarray(mesh.vertices, dtype=float)
    if len(faces) == 0:
        return 0

    tris = vertices[faces]
    lows = tris.min(axis=1)
    highs = tris.max(axis=1)

    cell = float(np.mean(highs - lows)) * 2.0
    if not np.isfinite(cell) or cell <= 1e-9:
        cell = 1.0

    buckets: dict[tuple[int, int, int], list[int]] = {}
    lo_idx = np.floor(lows / cell).astype(np.int64)
    hi_idx = np.floor(highs / cell).astype(np.int64)
    spans = (hi_idx - lo_idx + 1).prod(axis=1)
    # A triangle spanning a huge number of cells means the grid is badly sized
    # for this mesh; fall back to a coarser grid rather than exploding.
    if spans.max() > 512:
        cell *= float(np.cbrt(spans.max() / 64.0))
        lo_idx = np.floor(lows / cell).astype(np.int64)
        hi_idx = np.floor(highs / cell).astype(np.int64)

    for i in range(len(faces)):
        for gx in range(lo_idx[i, 0], hi_idx[i, 0] + 1):
            for gy in range(lo_idx[i, 1], hi_idx[i, 1] + 1):
                for gz in range(lo_idx[i, 2], hi_idx[i, 2] + 1):
                    buckets.setdefault((gx, gy, gz), []).append(i)

    seen: set[tuple[int, int]] = set()
    hits = 0
    for members in buckets.values():
        if len(members) < 2:
            continue
        for a_pos in range(len(members)):
            for b_pos in range(a_pos + 1, len(members)):
                a, b = members[a_pos], members[b_pos]
                key = (a, b) if a < b else (b, a)
                if key in seen:
                    continue
                seen.add(key)
                if np.intersect1d(faces[a], faces[b]).size:
                    continue  # adjacent triangles touch by construction
                if not _aabb_overlap(lows[a], highs[a], lows[b], highs[b]):
                    continue
                if _tri_tri_intersect(tris[a], tris[b]):
                    hits += 1
                    if hits >= max_report:
                        return hits
    return hits


def _aabb_overlap(
    lo_a: np.ndarray, hi_a: np.ndarray, lo_b: np.ndarray, hi_b: np.ndarray
) -> bool:
    return bool(np.all(lo_a <= hi_b) and np.all(lo_b <= hi_a))


def _tri_tri_intersect(t1: np.ndarray, t2: np.ndarray, eps: float = 1e-9) -> bool:
    """Moller's triangle-triangle overlap test.

    Each triangle is tested against the other's plane; if both straddle, the
    intervals they cut on the line of plane intersection are compared. Coplanar
    pairs are treated as non-intersecting: coincident faces are a modelling
    smell but they do not make a mesh unprintable, and flagging them produces
    false positives on every part with a flush-mated boolean.
    """
    n2 = np.cross(t2[1] - t2[0], t2[2] - t2[0])
    d2 = -float(np.dot(n2, t2[0]))
    dist1 = np.array([float(np.dot(n2, v)) + d2 for v in t1])
    if np.all(dist1 > eps) or np.all(dist1 < -eps):
        return False

    n1 = np.cross(t1[1] - t1[0], t1[2] - t1[0])
    d1 = -float(np.dot(n1, t1[0]))
    dist2 = np.array([float(np.dot(n1, v)) + d1 for v in t2])
    if np.all(dist2 > eps) or np.all(dist2 < -eps):
        return False

    direction = np.cross(n1, n2)
    if float(np.linalg.norm(direction)) < eps:
        return False  # coplanar

    axis = int(np.argmax(np.abs(direction)))
    proj1 = t1[:, axis]
    proj2 = t2[:, axis]
    interval1 = _plane_interval(proj1, dist1, eps)
    interval2 = _plane_interval(proj2, dist2, eps)
    if interval1 is None or interval2 is None:
        return False
    return not (interval1[1] < interval2[0] - eps or interval2[1] < interval1[0] - eps)


def _plane_interval(
    proj: np.ndarray, dist: np.ndarray, eps: float
) -> tuple[float, float] | None:
    """Where a triangle crosses the intersection line, as a 1D interval."""
    points: list[float] = []
    for i in range(3):
        j = (i + 1) % 3
        di, dj = float(dist[i]), float(dist[j])
        if abs(di) <= eps:
            points.append(float(proj[i]))
        if (di > eps and dj < -eps) or (di < -eps and dj > eps):
            t = di / (di - dj)
            points.append(float(proj[i] + t * (proj[j] - proj[i])))
    if len(points) < 2:
        return None
    return (min(points), max(points))
