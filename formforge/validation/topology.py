"""Tier 1: is this a valid solid at all? (spec section 7.1)

Everything here is a hard failure. A mesh that fails Tier 1 is not a model with
a problem, it is not a model -- a slicer will either reject it or produce
nonsense G-code, and no amount of DFM tuning downstream helps.

The parametric path should essentially never fail these checks, which is the
whole argument for the architecture (spec section 0). When it does, that is a
signal a boolean went wrong, and the right response is to regenerate rather than
to repair the mesh (spec section 7.4).
"""

from __future__ import annotations

from .mesh import MeshMeasurements
from .report import Severity, ValidationReport, check

TIER = 1


def run(measurements: MeshMeasurements, report: ValidationReport, *, expected_solids: int = 1) -> None:
    """Append every Tier 1 check to the report."""
    _watertight(measurements, report)
    _winding(measurements, report)
    _volume_sign(measurements, report)
    _degenerate(measurements, report)
    _duplicates(measurements, report)
    _self_intersection(measurements, report)
    _solid_count(measurements, report, expected_solids)
    _shards(measurements, report)
    _genus(measurements, report)


def _watertight(m: MeshMeasurements, report: ValidationReport) -> None:
    ok = m.is_watertight
    report.add(
        check(
            "topology.watertight",
            TIER,
            "Watertight",
            ok,
            message=(
                "The mesh is closed."
                if ok
                else "The mesh has boundary edges: it is not a closed solid and a "
                "slicer cannot determine what is inside it."
            ),
            measured=ok,
            threshold=True,
            remedy="A boolean or an extrude left an open surface. Rebuild the "
            "operation that created the opening rather than patching the mesh -- "
            "a hole in the mesh means the CAD logic is wrong, and a repaired mesh "
            "no longer matches the STEP file.",
        )
    )


def _winding(m: MeshMeasurements, report: ValidationReport) -> None:
    ok = m.is_winding_consistent
    report.add(
        check(
            "topology.winding",
            TIER,
            "Consistent winding",
            ok,
            message=(
                "Face winding is consistent."
                if ok
                else "Adjacent faces disagree about which side is outside."
            ),
            measured=ok,
            threshold=True,
            remedy="Normals are inconsistent, usually from a mirror or scale with "
            "a negative factor. Rebuild the mirrored geometry with an explicit "
            "mirror operation instead of a negative scale.",
        )
    )


def _volume_sign(m: MeshMeasurements, report: ValidationReport) -> None:
    volume = m.volume_mm3
    ok = volume > 0
    report.add(
        check(
            "topology.outward_normals",
            TIER,
            "Outward normals",
            ok,
            message=(
                f"Signed volume is positive ({volume:.1f} mm^3)."
                if ok
                else f"Signed volume is {volume:.1f} mm^3: the normals point inward, "
                "so the model describes the space around the object rather than "
                "the object."
            ),
            measured=round(volume, 3),
            threshold="> 0",
            unit="mm^3",
            remedy="Flip the solid's orientation, or rebuild the subtraction that "
            "inverted it.",
        )
    )


def _degenerate(m: MeshMeasurements, report: ValidationReport) -> None:
    count = m.degenerate_faces
    ok = count == 0
    report.add(
        check(
            "topology.degenerate_faces",
            TIER,
            "Degenerate faces",
            ok,
            message=(
                "No zero-area faces."
                if ok
                else f"{count} zero-area face(s): a slicer cannot compute a normal "
                "for these and may drop or corrupt the surrounding region."
            ),
            measured=count,
            threshold=0,
            remedy="Usually caused by coincident vertices in a sketch. Check for "
            "duplicated points in the profile, and for a fillet radius of exactly "
            "zero.",
        )
    )


def _duplicates(m: MeshMeasurements, report: ValidationReport) -> None:
    count = m.duplicate_vertices
    # Duplicates are repairable by merging without changing the geometry, so
    # they warn rather than fail -- the repair ladder's first rung handles them
    # and the STEP file is unaffected.
    ok = count == 0
    report.add(
        check(
            "topology.duplicate_vertices",
            TIER,
            "Duplicate vertices",
            ok,
            severity=Severity.WARN,
            message=(
                "No duplicate vertices."
                if ok
                else f"{count} vertices merge at a 1e-6 mm tolerance."
            ),
            measured=count,
            threshold=0,
            remedy="Harmless but wasteful; the repair pass will weld them.",
        )
    )


def _self_intersection(m: MeshMeasurements, report: ValidationReport) -> None:
    count = m.self_intersections
    if count is None:
        report.skipped.append("topology.self_intersection")
        return
    ok = count == 0
    capped = count >= 50
    report.add(
        check(
            "topology.self_intersection",
            TIER,
            "Self-intersection",
            ok,
            message=(
                "No self-intersecting faces."
                if ok
                else f"{'at least ' if capped else ''}{count} intersecting triangle "
                "pair(s): the surface passes through itself, so 'inside' is "
                "ambiguous."
            ),
            measured=count,
            threshold=0,
            remedy="A swept or lofted profile folded through itself, or two "
            "unioned solids overlap in a way the kernel resolved badly. Increase "
            "the radius of any tight turn on a sweep path to more than half the "
            "profile width.",
        )
    )


def _solid_count(m: MeshMeasurements, report: ValidationReport, expected: int) -> None:
    count = m.solid_count
    ok = count == expected
    report.add(
        check(
            "topology.solid_count",
            TIER,
            "Solid count",
            ok,
            message=(
                f"Exactly {count} solid, as expected."
                if ok
                else f"{count} disconnected solids, expected {expected}."
            ),
            measured=count,
            threshold=expected,
            remedy="Parts of the model are not joined. Either the features do not "
            "actually touch (check their positions overlap by at least 0.01 mm) "
            "or a boolean union was omitted.",
        )
    )


def _shards(m: MeshMeasurements, report: ValidationReport) -> None:
    shards = m.stray_shards
    ok = not shards
    location = shards[0]["centroid_mm"] if shards else None
    report.add(
        check(
            "topology.stray_shards",
            TIER,
            "Stray shards",
            ok,
            message=(
                "No zero-volume debris."
                if ok
                else f"{len(shards)} fragment(s) under 1 mm^3 left over from a "
                "boolean operation."
            ),
            measured=len(shards),
            threshold=0,
            location_mm=location,
            remedy="A boolean left slivers behind. Offset one operand by 0.01 mm so "
            "the faces are not exactly coplanar, then re-run the operation.",
        )
    )


def _genus(m: MeshMeasurements, report: ValidationReport) -> None:
    genus = m.genus
    # High genus is legitimate for lattices, so this is a sanity signal only.
    ok = genus <= 30
    report.add(
        check(
            "topology.genus",
            TIER,
            "Genus sanity",
            ok,
            severity=Severity.WARN,
            message=(
                f"Genus {genus}."
                if ok
                else f"Genus {genus} is unusually high for a functional part and "
                "usually means a boolean produced unintended tunnels."
            ),
            measured=genus,
            threshold=30,
            remedy="Expected for a lattice or a perforated pattern; suspicious "
            "otherwise. Check the render for holes you did not ask for.",
        )
    )
