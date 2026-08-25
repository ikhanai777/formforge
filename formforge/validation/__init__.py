"""Validation and DFM engine (spec section 7).

One entry point, `validate()`, runs three tiers in order and returns a single
report. Tier order matters: a mesh that fails Tier 1 has no meaningful wall
thickness, so Tier 2 is skipped rather than run on nonsense and allowed to
produce a second, misleading failure. One real failure beats five derived ones
when the report is about to become a repair prompt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..dfm import DFMLimits, get_profile, limits_for
from . import invariants, printability, topology
from .mesh import MeshMeasurements
from .report import Check, Severity, ValidationReport, check

__all__ = [
    "Check",
    "MeshMeasurements",
    "Severity",
    "ValidationReport",
    "validate",
    "validate_mesh",
]


def validate(
    mesh_path: str | Path,
    *,
    profile_id: str | None = None,
    material: str = "PLA",
    category: str | None = None,
    params: dict[str, Any] | None = None,
    template_invariants: list[str] | None = None,
    expected_solids: int = 1,
    brep_features: dict | None = None,
    text_features: list[dict] | None = None,
    requested_dimensions: dict[str, float] | None = None,
    limits: DFMLimits | None = None,
) -> ValidationReport:
    """Run the full DFM suite against an exported mesh."""
    profile = get_profile(profile_id)
    resolved = limits or limits_for(profile, material)
    report = ValidationReport(profile_id=profile.id, material=(material or "PLA").upper())

    try:
        measurements = MeshMeasurements.from_file(mesh_path)
    except Exception as exc:
        report.add(
            check(
                "topology.loadable",
                1,
                "Mesh loads",
                False,
                message=f"The exported mesh could not be read: {exc}",
                remedy="The export step produced a file no mesh library can parse. "
                "This is an export failure, not a design failure; regenerate.",
            )
        )
        return report

    return validate_mesh(
        measurements,
        report=report,
        limits=resolved,
        material=material,
        category=category,
        params=params or {},
        template_invariants=template_invariants,
        expected_solids=expected_solids,
        brep_features=brep_features,
        text_features=text_features,
        requested_dimensions=requested_dimensions,
    )


def validate_mesh(
    measurements: MeshMeasurements,
    *,
    report: ValidationReport | None = None,
    limits: DFMLimits | None = None,
    material: str = "PLA",
    category: str | None = None,
    params: dict[str, Any] | None = None,
    template_invariants: list[str] | None = None,
    expected_solids: int = 1,
    brep_features: dict | None = None,
    text_features: list[dict] | None = None,
    requested_dimensions: dict[str, float] | None = None,
) -> ValidationReport:
    """Run the suite against already-loaded measurements."""
    report = report or ValidationReport(material=(material or "PLA").upper())
    resolved = limits or limits_for(None, material)
    params = params or {}

    topology.run(measurements, report, expected_solids=expected_solids)
    tier1_passed = report.passed

    if tier1_passed:
        printability.run(
            measurements,
            report,
            resolved,
            brep_features=brep_features,
            text_features=text_features,
        )
        invariants.run(
            measurements,
            report,
            category=category,
            params=params,
            template_invariants=template_invariants,
            material=material,
            brep_features=brep_features,
        )
        if requested_dimensions:
            _dimensional_fidelity(measurements, report, requested_dimensions)
    else:
        report.skipped.append(
            "tier2_printability and tier3_invariants (skipped: the model is not a "
            "valid solid, so measurements taken from it would be meaningless)"
        )

    report.measurements.update(
        measurements.as_dict() if tier1_passed else measurements.basic_dict()
    )
    report.skipped.extend(measurements.skipped)
    outcome = measurements.repair_result
    if outcome is not None and getattr(outcome, "changed", False):
        # Recorded, not hidden. A user who compares the mesh to the STEP file
        # deserves to know something touched it, even when the change provably
        # moved nothing.
        report.measurements["auto_repair"] = outcome.as_dict()
    return report


def _dimensional_fidelity(
    m: MeshMeasurements,
    report: ValidationReport,
    requested: dict[str, float],
    tolerance: float = 0.02,
) -> None:
    """Does the model match the size the user actually asked for?

    The metric the benchmark scores (spec section 13.2) and the one users
    notice. A 2% tolerance is generous for a parametric kernel -- anything
    outside it means the script used the parameter for something other than the
    dimension it names, which no other check would catch.
    """
    extents = m.extents_mm
    axes = {"width_mm": 0, "length_mm": 0, "depth_mm": 1, "height_mm": 2}
    for key, index in axes.items():
        target = requested.get(key)
        if not target:
            continue
        actual = extents[index]
        error = abs(actual - float(target)) / float(target) if target else 0.0
        ok = error <= tolerance
        report.add(
            check(
                f"dimension.{key}",
                2,
                f"Requested {key.replace('_mm', '')}",
                ok,
                message=(
                    f"{key.replace('_mm', '').title()} is {actual:.1f} mm as requested."
                    if ok
                    else f"{key.replace('_mm', '').title()} came out {actual:.1f} mm, "
                    f"{error * 100:.1f}% off the requested {float(target):.1f} mm."
                ),
                measured=round(actual, 2),
                threshold=round(float(target), 2),
                unit="mm",
                remedy=f"The bounding box must match the requested dimension. Check "
                f"that {key.upper()} drives the overall size and is not, for "
                "example, an inner dimension with wall thickness added on top.",
            )
        )
