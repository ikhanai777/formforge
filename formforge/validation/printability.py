"""Tier 2: will this actually print? (spec section 7.2)

Tier 1 asks whether the mesh is a solid. Tier 2 asks whether a specific machine
can make it out of a specific material, so every threshold here comes from a
`DFMLimits` resolved for that pair rather than from a constant in this file.

The fail/warn split is a product decision, not a technical one. A hard failure
costs a repair iteration and delays the user; a warning that should have been a
failure costs them a spool of filament and eight hours. When in doubt, warn on
the aesthetic problems and fail on the ones that end with a part in the bin.
"""

from __future__ import annotations

from ..dfm import DFMLimits
from .mesh import MeshMeasurements
from .report import Severity, ValidationReport, check

TIER = 2


def run(
    m: MeshMeasurements,
    report: ValidationReport,
    limits: DFMLimits,
    *,
    brep_features: dict | None = None,
    text_features: list[dict] | None = None,
) -> None:
    """Append every Tier 2 check to the report."""
    _wall_thickness(m, report, limits)
    _feature_size(m, report, limits)
    _holes(report, limits, brep_features)
    _build_volume(m, report, limits)
    _overhangs(m, report, limits)
    _bridges(m, report, limits)
    _footprint(m, report, limits)
    _aspect_ratio(m, report, limits)
    _thin_tall_walls(m, report, limits)
    _trapped_volume(m, report)
    _text(report, limits, text_features)
    _triangle_count(m, report, limits)


def _wall_thickness(m: MeshMeasurements, report: ValidationReport, lim: DFMLimits) -> None:
    thickness = m.thickness
    if thickness is None:
        report.skipped.append("printability.wall_thickness")
        return

    report.measurements["min_wall_mm"] = round(thickness.min_mm, 3)
    report.measurements["median_wall_mm"] = round(thickness.median_mm, 3)

    # The 1st percentile rather than the raw minimum decides the hard failure.
    # A single sample landing in the tangent crease of a fillet reads thin and
    # prints fine; failing on it would reject good models forever. The absolute
    # minimum is still reported so the model knows where to look.
    representative = thickness.p01_mm
    hard_ok = representative >= lim.min_wall_fail_mm
    report.add(
        check(
            "printability.min_wall",
            TIER,
            "Minimum wall thickness",
            hard_ok,
            message=(
                f"Thinnest wall {thickness.min_mm:.2f} mm "
                f"(1st percentile {representative:.2f} mm)."
                if hard_ok
                else f"Walls down to {representative:.2f} mm, below the "
                f"{lim.min_wall_fail_mm:.2f} mm the nozzle can extrude. These "
                "regions will print as gaps or not at all."
            ),
            measured=round(representative, 3),
            threshold=lim.min_wall_fail_mm,
            unit="mm",
            location_mm=thickness.location_mm,
            remedy=f"Increase the wall parameter to at least "
            f"{lim.min_wall_warn_mm:.1f} mm. If the thin region is where a fillet "
            "meets a wall, reduce the fillet radius instead.",
        )
    )
    if hard_ok:
        soft_ok = representative >= lim.min_wall_warn_mm
        report.add(
            check(
                "printability.wall_strength",
                TIER,
                "Wall strength",
                soft_ok,
                severity=Severity.WARN,
                message=(
                    f"Walls are at least {representative:.2f} mm."
                    if soft_ok
                    else f"Walls down to {representative:.2f} mm print as two "
                    "perimeters with no infill between them: structurally weak "
                    "and prone to splitting along a layer line."
                ),
                measured=round(representative, 3),
                threshold=lim.min_wall_warn_mm,
                unit="mm",
                location_mm=thickness.location_mm,
                remedy=f"Three perimeters ({lim.min_wall_warn_mm:.1f} mm) is the "
                "practical minimum for anything that gets handled.",
            )
        )


def _feature_size(m: MeshMeasurements, report: ValidationReport, lim: DFMLimits) -> None:
    """Smallest standalone protrusion.

    Approximated from the thickness distribution: a protrusion thinner than the
    nozzle can lay down is measured as a thin wall by the inward ray cast. The
    approximation cannot separate "a thin pin" from "a thin wall", so this check
    only fires when the thin region is a small fraction of the surface -- which
    is what distinguishes an isolated feature from a uniformly thin shell.
    """
    thickness = m.thickness
    if thickness is None:
        report.skipped.append("printability.feature_size")
        return
    fraction_thin = thickness.fraction_below.get("0.8", 0.0)
    isolated = 0.0 < fraction_thin < 0.02
    ok = not (isolated and thickness.min_mm < lim.min_feature_mm)
    report.add(
        check(
            "printability.min_feature",
            TIER,
            "Minimum feature size",
            ok,
            message=(
                "No sub-nozzle standalone features."
                if ok
                else f"An isolated feature measures {thickness.min_mm:.2f} mm, under "
                f"the {lim.min_feature_mm:.2f} mm minimum. It will not form."
            ),
            measured=round(thickness.min_mm, 3),
            threshold=lim.min_feature_mm,
            unit="mm",
            location_mm=thickness.location_mm,
            remedy="Thicken the feature, or remove it and engrave the detail "
            "instead of embossing it.",
        )
    )


def _holes(report: ValidationReport, lim: DFMLimits, features: dict | None) -> None:
    """Hole diameters, read exactly from the B-rep's cylindrical faces.

    Holes print undersized by 0.1-0.2 mm because the extrusion bulges inward on
    an inside corner, so a hole specified at the minimum is already at the edge.
    """
    if not features or not features.get("cylinders"):
        report.skipped.append("printability.hole_diameter")
        return

    holes = [c for c in features["cylinders"] if c.get("internal")]
    if not holes:
        report.add(
            check("printability.hole_diameter", TIER, "Hole diameter", True,
                  message="No cylindrical holes in the model.")
        )
        return

    smallest = min(holes, key=lambda c: c["diameter_mm"])
    diameter = float(smallest["diameter_mm"])
    ok = diameter >= lim.min_hole_mm
    report.measurements["min_hole_diameter_mm"] = round(diameter, 3)
    report.measurements["hole_count"] = len(holes)
    report.add(
        check(
            "printability.hole_diameter",
            TIER,
            "Hole diameter",
            ok,
            severity=Severity.WARN,
            message=(
                f"Smallest hole is {diameter:.2f} mm across ({len(holes)} holes)."
                if ok
                else f"Smallest hole is {diameter:.2f} mm, under the "
                f"{lim.min_hole_mm:.1f} mm minimum. It will close up or need "
                "drilling out."
            ),
            measured=round(diameter, 3),
            threshold=lim.min_hole_mm,
            unit="mm",
            remedy="Enlarge the hole. If it has to fit hardware, add 0.2 mm to "
            "the nominal size to compensate for the undersize.",
        )
    )


def _build_volume(m: MeshMeasurements, report: ValidationReport, lim: DFMLimits) -> None:
    extents = m.extents_mm
    volume = lim.build_volume_mm
    # Compare sorted extents so a part that fits when rotated on the plate is
    # not rejected for being wide in the wrong axis.
    fits = all(e <= b + 1e-6 for e, b in zip(sorted(extents[:2]), sorted(volume[:2]))) and (
        extents[2] <= volume[2] + 1e-6
    )
    report.measurements["bbox_mm"] = [round(v, 2) for v in extents]
    report.add(
        check(
            "printability.build_volume",
            TIER,
            "Build volume",
            fits,
            message=(
                f"{extents[0]:.0f} x {extents[1]:.0f} x {extents[2]:.0f} mm fits the "
                f"{volume[0]:.0f} x {volume[1]:.0f} x {volume[2]:.0f} mm plate."
                if fits
                else f"{extents[0]:.0f} x {extents[1]:.0f} x {extents[2]:.0f} mm "
                f"exceeds the {volume[0]:.0f} x {volume[1]:.0f} x {volume[2]:.0f} mm "
                "build volume."
            ),
            measured=[round(v, 1) for v in extents],
            threshold=[round(v, 1) for v in volume],
            unit="mm",
            remedy="Scale the part down, or split it into sections with alignment "
            "pins and print them separately.",
        )
    )


def _overhangs(m: MeshMeasurements, report: ValidationReport, lim: DFMLimits) -> None:
    overhangs = m.overhangs
    ok = overhangs.fraction <= lim.overhang_area_warn_frac
    report.measurements["overhang_fraction"] = round(overhangs.fraction, 4)
    report.measurements["max_overhang_deg"] = round(overhangs.max_angle_deg, 1)
    report.add(
        check(
            "printability.overhang",
            TIER,
            "Overhang area",
            ok,
            severity=Severity.WARN,
            message=(
                f"{overhangs.fraction * 100:.1f}% of the surface overhangs past "
                f"{lim.overhang_limit_deg:.0f} degrees."
                if ok
                else f"{overhangs.fraction * 100:.1f}% of the surface overhangs past "
                f"{lim.overhang_limit_deg:.0f} degrees (steepest "
                f"{overhangs.max_angle_deg:.0f} degrees). This part needs supports."
            ),
            measured=round(overhangs.fraction, 4),
            threshold=lim.overhang_area_warn_frac,
            location_mm=overhangs.worst_location_mm,
            remedy="Reorient the part so the overhanging faces point up, or "
            "chamfer them to 45 degrees so they self-support.",
        )
    )


def _bridges(m: MeshMeasurements, report: ValidationReport, lim: DFMLimits) -> None:
    bridges = m.bridges
    span = bridges.max_span_mm
    report.measurements["max_bridge_mm"] = round(span, 2)
    hard_ok = span <= lim.max_bridge_fail_mm
    report.add(
        check(
            "printability.bridge_span",
            TIER,
            "Bridge span",
            hard_ok,
            message=(
                f"Longest unsupported span is {span:.1f} mm."
                if hard_ok
                else f"A {span:.1f} mm unsupported span exceeds the "
                f"{lim.max_bridge_fail_mm:.0f} mm a bridge can cross. It will sag "
                "into the cavity below."
            ),
            measured=round(span, 2),
            threshold=lim.max_bridge_fail_mm,
            unit="mm",
            location_mm=bridges.location_mm,
            remedy="Add a chamfered transition under the span so it steps up in "
            "45-degree increments, or split the span with a rib.",
        )
    )
    if hard_ok:
        soft_ok = span <= lim.max_bridge_warn_mm
        report.add(
            check(
                "printability.bridge_quality",
                TIER,
                "Bridge quality",
                soft_ok,
                severity=Severity.WARN,
                message=(
                    f"Longest span {span:.1f} mm bridges cleanly."
                    if soft_ok
                    else f"A {span:.1f} mm span will bridge but the underside will "
                    "be rough and may droop a layer."
                ),
                measured=round(span, 2),
                threshold=lim.max_bridge_warn_mm,
                unit="mm",
                location_mm=bridges.location_mm,
                remedy="Acceptable on a hidden face; chamfer it if the underside "
                "is visible.",
            )
        )


def _footprint(m: MeshMeasurements, report: ValidationReport, lim: DFMLimits) -> None:
    footprint = m.footprint
    report.measurements["plate_contact_fraction"] = round(footprint.contact_fraction, 4)

    adhesion_ok = footprint.contact_fraction >= lim.footprint_warn_frac
    report.add(
        check(
            "printability.bed_adhesion",
            TIER,
            "Bed adhesion",
            adhesion_ok,
            severity=Severity.WARN,
            message=(
                f"First layer covers {footprint.contact_fraction * 100:.0f}% of the "
                "footprint."
                if adhesion_ok
                else f"First layer covers only "
                f"{footprint.contact_fraction * 100:.1f}% of the footprint "
                f"({footprint.contact_area_mm2:.0f} mm^2). The part is likely to "
                "come loose mid-print."
            ),
            measured=round(footprint.contact_fraction, 4),
            threshold=lim.footprint_warn_frac,
            remedy="Add a flat base or a brim recommendation to the print "
            "metadata, or reorient the part to put a larger face on the bed.",
        )
    )

    stable = footprint.com_inside_footprint and footprint.com_margin_frac <= 0.8
    report.add(
        check(
            "printability.tipping",
            TIER,
            "Tipping stability",
            stable,
            severity=Severity.WARN,
            message=(
                "Centre of mass sits well inside the footprint."
                if stable
                else "The centre of mass projects near or outside the base. The "
                "part is unstable both on the plate and once printed."
            ),
            measured=round(footprint.com_margin_frac, 3),
            threshold=0.8,
            location_mm=footprint.com_mm,
            remedy="Widen the base, or move mass lower in the part.",
        )
    )


def _aspect_ratio(m: MeshMeasurements, report: ValidationReport, lim: DFMLimits) -> None:
    ratio = m.aspect_ratio
    ok = ratio <= lim.aspect_ratio_warn
    report.measurements["aspect_ratio"] = round(ratio, 2)
    report.add(
        check(
            "printability.aspect_ratio",
            TIER,
            "Aspect ratio",
            ok,
            severity=Severity.WARN,
            message=(
                f"Height to base ratio {ratio:.1f}:1."
                if ok
                else f"Height to base ratio {ratio:.1f}:1 exceeds "
                f"{lim.aspect_ratio_warn:.0f}:1. Tall narrow parts wobble under the "
                "print head and are prone to layer shifts."
            ),
            measured=round(ratio, 2),
            threshold=lim.aspect_ratio_warn,
            remedy="Widen the base, print the part lying down, or recommend a "
            "slower print speed in the metadata.",
        )
    )


def _thin_tall_walls(m: MeshMeasurements, report: ValidationReport, lim: DFMLimits) -> None:
    thickness = m.thickness
    if thickness is None:
        return
    height = m.extents_mm[2]
    risky = (
        thickness.median_mm < lim.thin_tall_wall_mm and height > lim.thin_tall_height_mm
    )
    report.add(
        check(
            "printability.thin_tall_wall",
            TIER,
            "Thin tall walls",
            not risky,
            severity=Severity.WARN,
            message=(
                "Wall thickness is proportionate to the part height."
                if not risky
                else f"A {thickness.median_mm:.1f} mm wall carried {height:.0f} mm up "
                "will flex during printing and ring on fast moves."
            ),
            measured=round(thickness.median_mm, 2),
            threshold=lim.thin_tall_wall_mm,
            unit="mm",
            remedy="Thicken the wall, or add a rib or a taper at the base.",
        )
    )


def _trapped_volume(m: MeshMeasurements, report: ValidationReport) -> None:
    trapped = m.trapped_volumes
    ok = not trapped
    total = sum(t["volume_mm3"] for t in trapped)
    report.add(
        check(
            "printability.trapped_volume",
            TIER,
            "Trapped volume",
            ok,
            message=(
                "No fully enclosed voids."
                if ok
                else f"{len(trapped)} enclosed void(s) totalling {total:.0f} mm^3 with "
                "no path to the outside."
            ),
            measured=len(trapped),
            threshold=0,
            location_mm=trapped[0]["centroid_mm"] if trapped else None,
            remedy="Add a drain hole, or make the cavity open. A sealed void wastes "
            "material in FDM and traps uncured resin in SLA.",
        )
    )


def _text(report: ValidationReport, lim: DFMLimits, features: list[dict] | None) -> None:
    """Text legibility, from the declared text parameters.

    Measuring stroke width off a tessellated glyph is possible but fragile, and
    the numbers that matter are already known: the template or the generated
    script declared the font size and the relief depth. Checking the declaration
    is both exact and free.
    """
    if not features:
        return
    for i, feature in enumerate(features):
        label = feature.get("label") or feature.get("text") or f"text {i + 1}"
        cap = float(feature.get("cap_height_mm", feature.get("font_size_mm", 0)) or 0)
        depth = float(feature.get("depth_mm", 0) or 0)
        stroke = float(feature.get("stroke_mm", cap * 0.18) or 0)

        problems: list[str] = []
        if cap and cap < lim.text_min_cap_mm:
            problems.append(f"cap height {cap:.1f} mm under {lim.text_min_cap_mm:.0f} mm")
        if depth and depth < lim.text_min_depth_mm:
            problems.append(f"relief depth {depth:.2f} mm under {lim.text_min_depth_mm:.1f} mm")
        if stroke and stroke < lim.text_min_stroke_mm:
            problems.append(
                f"stroke width {stroke:.2f} mm under {lim.text_min_stroke_mm:.1f} mm"
            )

        ok = not problems
        report.add(
            check(
                f"printability.text_legibility[{i}]",
                TIER,
                f"Text legibility ({label})",
                ok,
                message=(
                    f"Text '{label}' is legible at this size."
                    if ok
                    else f"Text '{label}' will not read: " + "; ".join(problems) + "."
                ),
                measured={"cap_mm": cap, "depth_mm": depth, "stroke_mm": round(stroke, 2)},
                threshold={
                    "cap_mm": lim.text_min_cap_mm,
                    "depth_mm": lim.text_min_depth_mm,
                    "stroke_mm": lim.text_min_stroke_mm,
                },
                remedy=f"Raise the font size to at least {lim.text_min_cap_mm:.0f} mm "
                "and use a medium or bold sans-serif. Below that, engrave rather "
                "than emboss.",
            )
        )


def _triangle_count(m: MeshMeasurements, report: ValidationReport, lim: DFMLimits) -> None:
    count = m.triangle_count
    report.measurements["triangles"] = count
    hard_ok = count <= lim.triangles_fail
    report.add(
        check(
            "printability.triangle_count",
            TIER,
            "Mesh size",
            hard_ok,
            message=(
                f"{count} triangles."
                if hard_ok
                else f"{count} triangles exceeds the {lim.triangles_fail} cap; the "
                "file will be slow to slice and awkward to download."
            ),
            measured=count,
            threshold=lim.triangles_fail,
            remedy="Coarsen the tessellation deflection, or simplify the geometry.",
        )
    )
    if hard_ok and count > lim.triangles_warn:
        report.add(
            check(
                "printability.mesh_weight",
                TIER,
                "Mesh weight",
                False,
                severity=Severity.WARN,
                message=f"{count} triangles is heavier than necessary for this part.",
                measured=count,
                threshold=lim.triangles_warn,
                remedy="A 0.05 mm deflection is finer than a 0.2 mm layer can "
                "reproduce; coarsening it costs nothing visible.",
            )
        )
