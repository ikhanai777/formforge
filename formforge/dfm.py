"""Design-for-manufacturing constants and the cached prompt fragment built from them.

Spec section 8. Every number here is a product decision, not an implementation
detail: these constants are what the validation engine enforces and what the
codegen prompt tells the model to respect. Keeping them in one place means the
rules block and the validator can never disagree, which is the failure mode that
makes an agent loop spin forever -- generating geometry the prompt permitted and
the validator rejects.

Tune against physical print results (spec section 13.3), not intuition.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class PrinterProfile:
    """An FDM machine + material combination the DFM rules are expressed against."""

    id: str
    display_name: str
    nozzle_mm: float
    layer_mm: float
    build_volume_mm: tuple[float, float, float]
    material: str = "PLA"
    # Slicer profile filename, relative to the profiles/ directory.
    slicer_profile: str | None = None

    @property
    def min_wall_hard_mm(self) -> float:
        """Two perimeters. Below this the wall is not reliably extruded at all."""
        return round(self.nozzle_mm * 2, 3)

    @property
    def min_wall_warn_mm(self) -> float:
        """Three perimeters. Below this the wall exists but is fragile."""
        return round(self.nozzle_mm * 3, 3)

    @property
    def min_feature_mm(self) -> float:
        """A standalone protrusion narrower than 1.5 nozzles will not stand up."""
        return round(self.nozzle_mm * 1.5, 3)

    @property
    def min_floor_mm(self) -> float:
        """Five layers, the point at which a floor stops being translucent."""
        return round(self.layer_mm * 5, 3)


PROFILES: dict[str, PrinterProfile] = {
    p.id: p
    for p in [
        PrinterProfile(
            id="generic_fdm_0.4",
            display_name="Generic FDM, 0.4 mm nozzle",
            nozzle_mm=0.4,
            layer_mm=0.2,
            build_volume_mm=(220.0, 220.0, 250.0),
            slicer_profile="generic_fdm_0.4_pla_0.20.ini",
        ),
        PrinterProfile(
            id="generic_fdm_0.6",
            display_name="Generic FDM, 0.6 mm nozzle",
            nozzle_mm=0.6,
            layer_mm=0.3,
            build_volume_mm=(220.0, 220.0, 250.0),
            slicer_profile="generic_fdm_0.6_pla_0.30.ini",
        ),
        PrinterProfile(
            id="bambu_p1s_0.4",
            display_name="Bambu Lab P1S / A1, 0.4 mm nozzle",
            nozzle_mm=0.4,
            layer_mm=0.2,
            build_volume_mm=(256.0, 256.0, 256.0),
            slicer_profile="bambu_p1s_pla_0.20.ini",
        ),
        PrinterProfile(
            id="prusa_mk4_0.4",
            display_name="Prusa MK4, 0.4 mm nozzle",
            nozzle_mm=0.4,
            layer_mm=0.2,
            build_volume_mm=(250.0, 210.0, 220.0),
            slicer_profile="prusa_mk4_pla_0.20.ini",
        ),
        PrinterProfile(
            id="ender3_v3_0.4",
            display_name="Creality Ender 3 V3, 0.4 mm nozzle",
            nozzle_mm=0.4,
            layer_mm=0.2,
            build_volume_mm=(220.0, 220.0, 250.0),
            slicer_profile="ender3_v3_pla_0.20.ini",
        ),
        PrinterProfile(
            id="draft_fast_0.4",
            display_name="Draft / fast, 0.4 mm nozzle, 0.3 mm layer",
            nozzle_mm=0.4,
            layer_mm=0.3,
            build_volume_mm=(220.0, 220.0, 250.0),
            slicer_profile="generic_fdm_0.4_pla_0.30.ini",
        ),
    ]
}

DEFAULT_PROFILE_ID = "generic_fdm_0.4"


def get_profile(profile_id: str | None) -> PrinterProfile:
    """Look up a profile, falling back to the generic 0.4 mm machine."""
    if not profile_id:
        return PROFILES[DEFAULT_PROFILE_ID]
    try:
        return PROFILES[profile_id]
    except KeyError:
        raise KeyError(
            f"unknown printer profile {profile_id!r}; "
            f"known profiles: {', '.join(sorted(PROFILES))}"
        ) from None


# Material behaviour that shifts the thresholds. Multipliers apply to wall and
# feature minimums; bridging and overhang tolerance differ by how fast the
# material freezes.
@dataclass(frozen=True)
class MaterialProfile:
    id: str
    wall_scale: float = 1.0
    max_bridge_mm: float = 10.0
    max_bridge_hard_mm: float = 25.0
    overhang_limit_deg: float = 45.0
    notes: str = ""


MATERIALS: dict[str, MaterialProfile] = {
    "PLA": MaterialProfile("PLA", notes="Baseline. Rigid, low warp, good bridging."),
    "PETG": MaterialProfile(
        "PETG",
        wall_scale=1.1,
        max_bridge_mm=8.0,
        notes="Stringy, softer bridges; thicken walls slightly.",
    ),
    "ABS": MaterialProfile(
        "ABS",
        wall_scale=1.15,
        max_bridge_mm=8.0,
        notes="Warps. Needs an enclosure; prefer larger fillets at the base.",
    ),
    "ASA": MaterialProfile("ASA", wall_scale=1.15, max_bridge_mm=8.0, notes="As ABS, UV stable."),
    "TPU": MaterialProfile(
        "TPU",
        wall_scale=1.3,
        max_bridge_mm=5.0,
        overhang_limit_deg=40.0,
        notes="Flexible. Thin walls collapse under nozzle pressure.",
    ),
}


def get_material(material: str | None) -> MaterialProfile:
    return MATERIALS.get((material or "PLA").upper(), MATERIALS["PLA"])


@dataclass(frozen=True)
class DFMLimits:
    """Resolved numeric thresholds for one printer + material combination.

    The validation engine reads only this object, never PROFILES directly, so a
    caller can override any single threshold for a one-off check.
    """

    min_wall_fail_mm: float
    min_wall_warn_mm: float
    min_feature_mm: float
    min_hole_mm: float
    min_floor_mm: float
    max_bridge_warn_mm: float
    max_bridge_fail_mm: float
    overhang_limit_deg: float
    overhang_area_warn_frac: float
    footprint_warn_frac: float
    aspect_ratio_warn: float
    thin_tall_wall_mm: float
    thin_tall_height_mm: float
    text_min_stroke_mm: float
    text_min_depth_mm: float
    text_min_cap_mm: float
    triangles_warn: int
    triangles_fail: int
    build_volume_mm: tuple[float, float, float]
    # Clearances, spec section 8.
    fit_press_mm: float = 0.05
    fit_snug_mm: float = 0.15
    fit_free_mm: float = 0.25
    fit_lid_mm: float = 0.30
    genus_warn: int = 30
    min_solid_volume_mm3: float = 1.0

    def scaled(self, **overrides: float) -> "DFMLimits":
        return replace(self, **overrides)  # type: ignore[arg-type]


def limits_for(
    profile: PrinterProfile | str | None = None,
    material: str | None = None,
) -> DFMLimits:
    """Resolve the numeric DFM thresholds for a printer/material pair."""
    prof = profile if isinstance(profile, PrinterProfile) else get_profile(profile)
    mat = get_material(material)
    return DFMLimits(
        min_wall_fail_mm=round(prof.min_wall_hard_mm * mat.wall_scale, 3),
        min_wall_warn_mm=round(prof.min_wall_warn_mm * mat.wall_scale, 3),
        min_feature_mm=round(prof.min_feature_mm * mat.wall_scale, 3),
        min_hole_mm=2.0,
        min_floor_mm=prof.min_floor_mm,
        max_bridge_warn_mm=mat.max_bridge_mm,
        max_bridge_fail_mm=mat.max_bridge_hard_mm,
        overhang_limit_deg=mat.overhang_limit_deg,
        overhang_area_warn_frac=0.15,
        footprint_warn_frac=0.08,
        aspect_ratio_warn=6.0,
        thin_tall_wall_mm=2.0,
        thin_tall_height_mm=40.0,
        text_min_stroke_mm=max(1.0, prof.nozzle_mm * 2.5),
        text_min_depth_mm=max(0.4, prof.layer_mm * 2),
        text_min_cap_mm=3.0,
        triangles_warn=500_000,
        triangles_fail=2_000_000,
        build_volume_mm=prof.build_volume_mm,
    )


# --------------------------------------------------------------------------
# The prompt fragment.
# --------------------------------------------------------------------------

_RULES_TEMPLATE = """\
PRINTER: FDM, {nozzle} mm nozzle, {layer} mm layer, {material}, no supports preferred.
BUILD VOLUME: {bx} x {by} x {bz} mm.

DIMENSIONS
- Minimum wall thickness: {wall_warn} mm ({perims} perimeters). Use {wall_struct} mm
  structural, {wall_wet} mm for anything holding soil or water.
- Minimum standalone feature: {feature} mm.
- Minimum hole diameter: {hole} mm. Holes print undersized by ~0.1-0.2 mm;
  oversize holes that need to fit hardware by +0.2 mm.
- Floor thickness: minimum {floor} mm (5 layers). Use 1.6 mm for load bearing.

CLEARANCES (nominal, {nozzle} mm nozzle)
- Press fit:        {press} mm
- Snug sliding fit: {snug} mm
- Free/loose fit:   {free} mm
- Lid over box:     {lid} mm per side
- Screw shank clearance: nominal diameter + 0.4 mm
- M3 self-tapping into plastic: 2.6 mm pilot

GEOMETRY
- Overhangs: keep faces within {overhang}deg of vertical. Beyond that, add a
  chamfer, change orientation, or accept supports.
- Bridges: <= {bridge} mm unsupported. Longer spans need a chamfered
  transition or a support-free redesign.
- Fillet every internal corner (r >= 1.5 mm): stress risers and
  the printer can't make a sharp internal corner anyway.
- Chamfer the bottom edge 0.4-0.6 mm at 45deg to counter elephant's foot.
- First layer contact area >= {footprint}% of the XY bounding box, or add a brim
  recommendation to the metadata.

TEXT
- Cap height >= {cap} mm. Below that, use engraving not embossing.
- Stroke width >= {stroke} mm. Reject decorative/script fonts under 6 mm.
- Relief depth: emboss {depth} mm, engrave {depth} mm.
- Sans-serif, medium/bold weight. Never hairline.
- Text on a vertical face prints worse than text on the top face.
  Prefer top-face placement.
- CHECK MIRRORING. Text on a -X or -Y facing surface must be mirrored
  in the sketch, not in the final solid.

ORIENTATION
- Design for the part to print in the orientation that puts the
  largest flat face on the bed.
- Layer adhesion is the weak axis. Never orient a load-bearing
  cantilever so the load pulls layers apart -- a hook must print with
  its load path in-plane.
- State the intended print orientation in the 3MF metadata.

ALWAYS
- Declare every dimension as a named constant at module top.
- Assert the final bounding box against the requested dimensions.
- Produce exactly one solid unless multiple parts were requested.
{material_note}"""


def rules_block(
    profile: PrinterProfile | str | None = None,
    material: str | None = None,
) -> str:
    """Render the DFM rules prompt fragment for a printer/material pair.

    This string is the stable cached prefix of every codegen call, so it must be
    byte-identical between calls with the same arguments -- no timestamps, no
    dict iteration order, nothing that varies per request.
    """
    prof = profile if isinstance(profile, PrinterProfile) else get_profile(profile)
    mat = get_material(material)
    lim = limits_for(prof, material)
    note = f"\nMATERIAL NOTE ({mat.id}): {mat.notes}" if mat.notes else ""
    return _RULES_TEMPLATE.format(
        nozzle=_num(prof.nozzle_mm),
        layer=_num(prof.layer_mm),
        material=mat.id,
        bx=_num(prof.build_volume_mm[0]),
        by=_num(prof.build_volume_mm[1]),
        bz=_num(prof.build_volume_mm[2]),
        perims=3,
        wall_warn=_num(lim.min_wall_warn_mm),
        wall_struct=_num(max(1.6, lim.min_wall_warn_mm * 1.35)),
        wall_wet=_num(max(2.4, lim.min_wall_warn_mm * 2)),
        feature=_num(lim.min_feature_mm),
        hole=_num(lim.min_hole_mm),
        floor=_num(lim.min_floor_mm),
        press=_num(lim.fit_press_mm),
        snug=_num(lim.fit_snug_mm),
        free=_num(lim.fit_free_mm),
        lid=_num(lim.fit_lid_mm),
        overhang=_num(lim.overhang_limit_deg),
        bridge=_num(lim.max_bridge_warn_mm),
        footprint=_num(lim.footprint_warn_frac * 100),
        cap=_num(lim.text_min_cap_mm),
        stroke=_num(lim.text_min_stroke_mm),
        depth=_num(lim.text_min_depth_mm),
        material_note=note,
    )


def _num(value: float) -> str:
    """Format a float the way a spec sheet would: 2.4, not 2.4000000000000004."""
    rounded = round(float(value), 3)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:g}"


# Category-specific invariants layered on top of the generic rules
# (spec section 7.3). Templates may add their own; these always apply.
CATEGORY_RULES: dict[str, list[str]] = {
    "planter": [
        "Watertight below the soil line.",
        "Include drainage unless the user explicitly asked for none.",
        "Wall >= 2.0 mm below the soil line.",
        "The mount feature must be structurally continuous with the body.",
    ],
    "keychain": [
        "Ring hole >= 4 mm diameter with >= 2.5 mm of material on all sides.",
        "Overall thickness >= 2.5 mm.",
        "Text must read correctly, not mirrored, on the face it sits on.",
    ],
    "organizer": [
        "Flat bottom with >= 90% plate contact.",
        "Internal corners filleted r >= 1.5 mm.",
        "Stackable parts need 0.25-0.4 mm mating clearance.",
    ],
    "wall_decor": [
        "A hanging feature must be present.",
        "Keep any unsupported overhang region under 10 cm^2.",
        "Total mass under 400 g if adhesive-mounted.",
    ],
    "hook": [
        "Print with the load path in-plane; never pull layers apart.",
        "Screw bosses need >= 2 mm of material around the shank.",
    ],
    "box": [
        "Lid clearance 0.30 mm per side.",
        "Fillet internal corners r >= 1.5 mm.",
    ],
}

CATEGORIES = tuple(sorted(CATEGORY_RULES))


def category_rules_block(category: str | None) -> str:
    """Extra rules appended after the generic block for a known category."""
    rules = CATEGORY_RULES.get(category or "")
    if not rules:
        return ""
    lines = "\n".join(f"- {r}" for r in rules)
    return f"\n\nCATEGORY RULES ({category})\n{lines}"
