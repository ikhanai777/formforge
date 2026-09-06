"""Turn a traced outline into a manufacturable key holder layout.

The image contributes a shape. Everything that makes the shape a *product* is
decided here, in millimetres, against the same DFM rules the validator applies
afterwards:

* **The plate is one 2D polygon.** Silhouette, hook rail and mounting features
  are unioned in Shapely and extruded once. Doing the hard set operations in 2D
  rather than as 3D booleans is the single biggest robustness decision in this
  module: a traced photograph has a few hundred vertices and a self-intersection
  or two, and a kernel asked to union that against a bar in 3D fails in ways
  that are miserable to diagnose. Shapely fixes it in the plane, and OCCT is
  handed one clean profile.
* **Thin features are removed before they are printed, not reported after.**
  A morphological opening at half the minimum feature size deletes the necks and
  spikes a photo trace always produces. This is the step that turns "the
  validator says min wall 0.7 mm" into a part that just prints.
* **Print orientation is fixed: back flat on the plate.** An arbitrary
  silhouette has arbitrary curvature, and every one of those curves is a
  vertical wall in this orientation and an unsupported overhang in any other.
  The hooks are then shaped so that they, too, need no supports -- see
  `hook_profile`.

The cost of that orientation is honest and worth stating: the layer lines run
parallel to the wall, so the bending load at a hook root is carried across
layers rather than along them. That is why the hooks are wedges with a 45°
underside rather than slender pegs, and why the notes recommend PETG and four
perimeters for a rack that will hold heavy keys.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

from shapely import affinity
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.geometry.polygon import orient
from shapely.ops import unary_union
from shapely.prepared import prep

from ..dfm import PrinterProfile, get_profile

# How much of the hook's root is buried in the plate. A coincident face between
# the hook and the plate is exactly the boolean OCCT is worst at; one millimetre
# of overlap turns it into an ordinary volume union.
HOOK_EMBED_MM = 1.0

# Material left in front of a keyhole cavity. Under this the screw head shows
# through as a bulge on the visible face.
KEYHOLE_FRONT_WALL_MM = 1.2

# Material required around a mounting cut-out before it is allowed there.
MOUNT_MARGIN_MM = 2.5

# Target distance between hooks when the count is chosen automatically. A key
# fob is 25-30 mm across, so 38 mm keeps two neighbouring bunches from fouling.
HOOK_PITCH_TARGET_MM = 38.0

# Bounds every numeric parameter is checked against. Same contract as a
# template's param_schema: a value outside its range is refused up front rather
# than producing a part that validates and does not work.
PARAM_RANGES: dict[str, tuple[float, float]] = {
    "width_mm": (60.0, 300.0),
    "plaque_t_mm": (3.0, 12.0),
    "border_mm": (0.0, 12.0),
    # The floor is two perimeters on a 0.4 mm nozzle, the thinnest thing that
    # extrudes as a solid at all. A coarser nozzle raises it -- see
    # KeyHolderSpec.validate, which is where the printer gets a say.
    "min_feature_mm": (0.8, 8.0),
    "close_gaps_mm": (0.0, 6.0),
    "simplify_mm": (0.0, 2.0),
    "engrave_depth_mm": (0.2, 3.0),
    "rail_h_mm": (0.0, 60.0),
    "rail_overlap_mm": (0.0, 30.0),
    "rail_inset_mm": (0.0, 40.0),
    "rail_corner_r_mm": (0.0, 12.0),
    "hook_count": (0, 12),
    "hook_out_mm": (8.0, 45.0),
    "hook_w_mm": (4.0, 20.0),
    "hook_root_h_mm": (6.0, 40.0),
    "hook_lip_mm": (3.0, 25.0),
    "hook_tip_t_mm": (2.4, 12.0),
    "screw_d_mm": (2.5, 8.0),
    "keyhole_head_d_mm": (6.0, 16.0),
    "keyhole_head_h_mm": (1.5, 6.0),
    "keyhole_slot_len_mm": (5.0, 25.0),
    "keyhole_lip_mm": (1.0, 4.0),
}

DETAIL_MODES = ("engrave", "cut", "ignore")
MOUNT_MODES = ("keyhole", "screw", "none")


class DesignError(Exception):
    """The requested key holder cannot be built as asked. Always actionable."""


@dataclass(frozen=True)
class KeyHolderSpec:
    """Everything the user gets to choose.

    Defaults describe a 180 mm wide rack with five hooks: about the size of the
    wooden ones people buy, and comfortably inside every build volume in
    `PROFILES`.
    """

    width_mm: float = 180.0
    # 7 mm because a keyhole is a sandwich and the layers have to add up: a
    # 2.4 mm retaining lip, a 3 mm cavity for the screw head, and 1.2 mm of
    # material in front so the head does not print through the visible face.
    # Rounding the 6.6 mm that needs up to 7 beats explaining an automatic
    # thickening on every single run. `--mount screw` is happy at 4 mm.
    plaque_t_mm: float = 7.0
    # Grows the silhouette outwards. A small border turns a spindly trace into
    # a plate with a visible edge, and welds nearly-touching parts together.
    border_mm: float = 0.0
    min_feature_mm: float = 2.4
    close_gaps_mm: float = 0.8
    simplify_mm: float = 0.25

    # Interior openings found in the image: cut through, engrave into the face,
    # or ignore.
    detail: str = "engrave"
    engrave_depth_mm: float = 1.0

    rail: bool = True
    rail_h_mm: float = 18.0
    rail_overlap_mm: float = 4.0
    rail_inset_mm: float = 0.0
    rail_corner_r_mm: float = 3.0

    hook_count: int = 0  # 0 chooses a count from the width
    hook_out_mm: float = 20.0
    hook_w_mm: float = 8.0
    hook_root_h_mm: float = 15.0
    hook_lip_mm: float = 9.0
    hook_tip_t_mm: float = 4.0

    mount: str = "keyhole"
    screw_d_mm: float = 4.2
    keyhole_head_d_mm: float = 9.0
    keyhole_head_h_mm: float = 3.0
    keyhole_slot_len_mm: float = 12.0
    # Commercial printed keyholes use 1.5-2 mm here. This is the one wall in the
    # part that holds the whole thing on the wall, and it is thin in the weak
    # direction, so it gets the same 2.4 mm every other structural wall gets --
    # and, usefully, that makes a single minimum-wall rule true of the entire
    # model rather than true except in one place.
    keyhole_lip_mm: float = 2.4

    profile_id: str = "generic_fdm_0.4"
    material: str = "PLA"
    # Thicken the plate when a keyhole will not fit in it, rather than failing.
    auto_thicken: bool = True

    def validate(self) -> list[str]:
        """Range and enum problems, in the same shape a template reports them."""
        problems: list[str] = []
        for name, (low, high) in PARAM_RANGES.items():
            value = getattr(self, name)
            if value < low or value > high:
                problems.append(f"{name}: {value} is outside the tested range {low}-{high}")
        if self.detail not in DETAIL_MODES:
            problems.append(f"detail: {self.detail!r} is not one of {list(DETAIL_MODES)}")
        if self.mount not in MOUNT_MODES:
            problems.append(f"mount: {self.mount!r} is not one of {list(MOUNT_MODES)}")
        if self.rail and self.rail_h_mm < self.hook_root_h_mm + 2:
            problems.append(
                f"rail_h_mm: a {self.rail_h_mm} mm rail cannot back a "
                f"{self.hook_root_h_mm} mm hook root; it needs "
                f"{self.hook_root_h_mm + 2:g} mm"
            )
        if not self.rail and self.hook_count != 0:
            problems.append(
                "rail: hooks are mounted on the rail, so --no-rail requires "
                "--hooks 0"
            )
        if self.hook_tip_t_mm >= self.hook_root_h_mm + self.hook_lip_mm:
            problems.append(
                "hook_tip_t_mm: the tip cannot be thicker than the hook is tall"
            )
        if self.hook_lip_mm >= self.hook_out_mm:
            problems.append(
                "hook_lip_mm: the lip rises at 45 degrees, so it cannot be taller "
                "than the hook projects"
            )
        if self.min_feature_mm < self.nozzle_mm * 2:
            problems.append(
                f"min_feature_mm: {self.min_feature_mm} mm is under two "
                f"{self.nozzle_mm} mm perimeters; nothing thinner prints as a solid"
            )
        return problems

    @property
    def printer(self) -> PrinterProfile:
        return get_profile(self.profile_id)

    @property
    def nozzle_mm(self) -> float:
        return self.printer.nozzle_mm

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MountPoint:
    """One wall fixing, centred on the head opening."""

    x_mm: float
    y_mm: float

    def as_dict(self) -> dict[str, float]:
        return {"x_mm": round(self.x_mm, 2), "y_mm": round(self.y_mm, 2)}


@dataclass
class KeyHolderPlan:
    """A complete, checked layout. The emitter turns this into build123d."""

    spec: KeyHolderSpec
    outline: list[tuple[float, float]]
    holes: list[list[tuple[float, float]]] = field(default_factory=list)
    engrave: list[list[tuple[float, float]]] = field(default_factory=list)
    hook_x_mm: list[float] = field(default_factory=list)
    hook_profile: list[tuple[float, float]] = field(default_factory=list)
    hook_base_y_mm: float = 0.0
    mounts: list[MountPoint] = field(default_factory=list)
    mount: str = "none"
    plaque_t_mm: float = 6.0
    size_mm: tuple[float, float] = (0.0, 0.0)
    scale_mm_per_px: float = 1.0
    area_mm2: float = 0.0
    estimated_mass_g: float = 0.0
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def depth_mm(self) -> float:
        return self.plaque_t_mm + self.spec.hook_out_mm if self.hook_x_mm else self.plaque_t_mm

    def bounding_box_mm(self) -> tuple[float, float, float]:
        return (self.size_mm[0], self.size_mm[1], self.depth_mm)

    def as_dict(self) -> dict[str, Any]:
        return {
            "size_mm": [round(v, 2) for v in self.bounding_box_mm()],
            "plaque_t_mm": round(self.plaque_t_mm, 2),
            "hooks": len(self.hook_x_mm),
            "hook_x_mm": [round(x, 2) for x in self.hook_x_mm],
            "hook_out_mm": self.spec.hook_out_mm,
            "mount": self.mount,
            "mounts": [m.as_dict() for m in self.mounts],
            "detail": self.spec.detail,
            "holes_cut": len(self.holes),
            "engraved_regions": len(self.engrave),
            "outline_points": len(self.outline),
            "area_mm2": round(self.area_mm2, 1),
            "estimated_mass_g": round(self.estimated_mass_g, 1),
            "scale_mm_per_px": round(self.scale_mm_per_px, 4),
            "notes": list(self.notes),
            "warnings": list(self.warnings),
        }

    def summary(self) -> str:
        hooks = f"{len(self.hook_x_mm)} hook(s)" if self.hook_x_mm else "no hooks"
        w, h, d = self.bounding_box_mm()
        return (
            f"{w:.0f} x {h:.0f} x {d:.0f} mm, {hooks}, {self.mount} mount, "
            f"~{self.estimated_mass_g:.0f} g"
        )


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------


def hook_profile(spec: KeyHolderSpec) -> list[tuple[float, float]]:
    """The hook's side view, in (out-from-wall, up-the-wall) millimetres.

    Printed with the plate flat on the bed, "out from wall" is the build
    direction, so every rising edge of this profile is an overhang and every
    horizontal one is a vertical wall. The profile is therefore built to two
    rules:

      * the lip rises at exactly 45°, the steepest self-supporting angle;
      * the underside falls at 45° or shallower, which also makes it the gusset
        that carries the load into the plate.

    The result needs no supports anywhere, which matters more here than
    anywhere else in the part: supports inside a hook are unreachable with
    pliers.
    """
    out = spec.hook_out_mm
    root = spec.hook_root_h_mm
    lip = spec.hook_lip_mm
    tip = spec.hook_tip_t_mm

    top_of_lip = root + lip
    bottom_at_tip = top_of_lip - tip
    # Where the 45° underside meets the plate. Clamped at zero: a short hook
    # simply has a shallower underside, which is still self-supporting.
    root_bottom = max(0.0, bottom_at_tip - out)

    return [
        (-HOOK_EMBED_MM, root_bottom),
        (-HOOK_EMBED_MM, root),
        (out - lip, root),
        (out, top_of_lip),
        (out, bottom_at_tip),
    ]


# ---------------------------------------------------------------------------
# Plate assembly
# ---------------------------------------------------------------------------


def _largest(geometry) -> Polygon:
    if isinstance(geometry, Polygon):
        return geometry
    if isinstance(geometry, MultiPolygon):
        if geometry.is_empty:
            raise DesignError("the outline collapsed to nothing")
        return max(geometry.geoms, key=lambda g: g.area)
    raise DesignError("the outline is not an area")


def _clean(polygon: Polygon, spec: KeyHolderSpec):
    """Close hairline gaps, then remove features thinner than the nozzle can print.

    Order matters twice over. Closing first merges parts of the subject that the
    trace separated by a sliver, so the opening that follows does not delete a
    limb that was about to be reattached. And the result is returned as it comes
    -- one piece or several -- rather than reduced to its largest part here:
    the opening routinely cuts a shape into lobes that the hook rail then joins
    back together, and dropping them before the rail exists would throw away
    most of a barbell-shaped silhouette that was about to be perfectly fine.
    """
    notes: list[str] = []
    result = polygon
    if spec.close_gaps_mm > 0:
        radius = spec.close_gaps_mm / 2.0
        result = result.buffer(radius, quad_segs=8).buffer(-radius, quad_segs=8)
    if spec.border_mm > 0:
        result = result.buffer(spec.border_mm, quad_segs=8)
        notes.append(f"grew the silhouette by a {spec.border_mm:g} mm border")

    before = result.area
    radius = spec.min_feature_mm / 2.0
    opened = result.buffer(-radius, quad_segs=8).buffer(radius, quad_segs=8)
    if opened.is_empty or opened.area <= 0:
        raise DesignError(
            f"nothing survives a {spec.min_feature_mm:g} mm minimum feature size at "
            f"{spec.width_mm:g} mm wide: the whole silhouette is thinner than that. "
            "Make it wider, lower --min-feature, or use a chunkier image."
        )
    lost = 1.0 - opened.area / max(before, 1e-9)
    if lost > 0.02:
        notes.append(
            f"removed features thinner than {spec.min_feature_mm:g} mm "
            f"({lost:.0%} of the traced area)"
        )
    result = opened

    if not result.is_valid:
        result = result.buffer(0)
    return result, notes


def _rounded_box(x0: float, y0: float, x1: float, y1: float, radius: float) -> Polygon:
    rect = box(x0, y0, x1, y1)
    if radius <= 0:
        return rect
    radius = min(radius, (x1 - x0) / 2 - 0.01, (y1 - y0) / 2 - 0.01)
    if radius <= 0:
        return rect
    return rect.buffer(-radius, quad_segs=8).buffer(radius, quad_segs=8)


def _connected_to(plate, anchor: Polygon) -> tuple[Polygon, float]:
    """Keep only the piece that includes the anchor, and say what that cost."""
    if isinstance(plate, Polygon):
        return plate, 0.0
    total = plate.area
    keep = [g for g in plate.geoms if g.intersects(anchor)]
    if not keep:
        keep = [max(plate.geoms, key=lambda g: g.area)]
    winner = max(keep, key=lambda g: g.area)
    return winner, 1.0 - winner.area / max(total, 1e-9)


def _simplify(geometry, tolerance: float):
    if tolerance <= 0:
        return geometry
    simplified = geometry.simplify(tolerance, preserve_topology=True)
    if simplified.is_empty or not simplified.is_valid:
        return geometry
    return simplified


# ---------------------------------------------------------------------------
# Mounts
# ---------------------------------------------------------------------------


def mount_radius(spec: KeyHolderSpec) -> float:
    """Half the width of the widest part of a fixing cut-out."""
    if spec.mount == "keyhole":
        return spec.keyhole_head_d_mm / 2.0
    return max(spec.screw_d_mm, spec.keyhole_head_d_mm) / 2.0


def mount_footprint(spec: KeyHolderSpec, x: float, y: float) -> Polygon:
    """The 2D area a fixing occupies, for reporting and for the trace preview."""
    radius = mount_radius(spec)
    if spec.mount != "keyhole":
        return Point(x, y).buffer(radius, quad_segs=16)
    return LineString([(x, y), (x, y + spec.keyhole_slot_len_mm)]).buffer(
        radius, quad_segs=16
    )


def _place_mounts(
    plate: Polygon,
    spec: KeyHolderSpec,
    *,
    rail_top_y: float | None,
) -> tuple[list[MountPoint], list[str]]:
    """Find two fixings at the same height, as high up the piece as they fit.

    Same height matters: two screws at different heights make the piece hang
    crooked no matter how carefully they are drilled. Height matters because a
    piece hung near its top lies flat against the wall, where one hung near its
    bottom pivots off it when the hooks are loaded.

    Anything that cannot take a fixing at all falls back to the rail, which is
    the one region of the plate this module drew itself and can guarantee.

    A keyhole's outline is exactly a disc swept along the slot, so "does it fit
    with 2.5 mm of material round it" is the same question as "is the slot's
    centre line inside the plate eroded by the head radius plus the margin".
    Eroding once and testing line segments turns a few thousand polygon
    intersections into a few thousand point-in-polygon tests.
    """
    notes: list[str] = []
    if spec.mount == "none":
        return [], notes

    minx, miny, maxx, maxy = plate.bounds
    height = maxy - miny
    width = maxx - minx

    safe = plate.buffer(-(mount_radius(spec) + MOUNT_MARGIN_MM), quad_segs=8)
    if safe.is_empty:
        notes.append(
            "no fixing fits anywhere in this shape with "
            f"{MOUNT_MARGIN_MM:g} mm of material around it, so the piece was "
            "built without one: mount it with adhesive strips, or re-run with "
            "--border to thicken the outline"
        )
        return [], notes
    ready = prep(safe)
    slot_len = spec.keyhole_slot_len_mm if spec.mount == "keyhole" else 0.0

    def fits(x: float, y: float) -> bool:
        if slot_len <= 0:
            return ready.contains(Point(x, y))
        return ready.contains(LineString([(x, y), (x, y + slot_len)]))

    step = max(1.5, width / 120.0)
    y_step = max(1.5, height / 120.0)
    # Search from the top down. Stop at the rail: a fixing inside the rail is
    # the fallback, not the preference, because the hooks load it.
    y_floor = (rail_top_y if rail_top_y is not None else miny) + 2.0
    y = maxy - 2.0
    best_single: MountPoint | None = None
    best_pair: tuple[float, float, float] | None = None  # spread, x_left, y
    stop_below: float | None = None
    min_spread = max(30.0, width * 0.25)

    while y > y_floor:
        if stop_below is not None and y < stop_below:
            break
        candidates = []
        x = minx + 2.0
        while x < maxx - 2.0:
            if fits(x, y):
                candidates.append(x)
            x += step
        if candidates:
            if best_single is None:
                best_single = MountPoint((candidates[0] + candidates[-1]) / 2.0, y)
            spread = candidates[-1] - candidates[0]
            if len(candidates) >= 2 and spread >= min_spread:
                if best_pair is None or spread > best_pair[0] * 1.25:
                    best_pair = (spread, candidates[0], y)
                if stop_below is None:
                    # The first workable height wins unless dropping a little
                    # lower buys a much wider stance. Higher is better for
                    # hanging flat; wider is better against rotation, and a
                    # quarter more spread is worth 15% of the height.
                    stop_below = y - height * 0.15
        y -= y_step

    if best_pair is not None:
        spread, left, y = best_pair
        return [MountPoint(left, y), MountPoint(left + spread, y)], notes

    if best_single is not None:
        notes.append(
            "only one fixing fits inside the silhouette, so the piece hangs on a "
            "single screw and can rotate; a second screw or a blob of putty at "
            "one corner stops it"
        )
        return [best_single], notes

    if rail_top_y is not None:
        rail_y = rail_top_y - spec.keyhole_slot_len_mm - spec.keyhole_head_d_mm / 2.0 - 2.0
        inset = max(15.0, width * 0.12)
        pair = [
            MountPoint(minx + inset, rail_y),
            MountPoint(maxx - inset, rail_y),
        ]
        if all(fits(m.x_mm, m.y_mm) for m in pair):
            notes.append(
                "the silhouette has no region solid enough for a fixing, so both "
                "keyholes went into the hook rail"
            )
            return pair, notes

    notes.append(
        "no fixing fits anywhere in this shape with "
        f"{MOUNT_MARGIN_MM:g} mm of material around it, so the piece was built "
        "without one: mount it with adhesive strips, or re-run with --border to "
        "thicken the outline"
    )
    return [], notes


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------


def plan(
    silhouette_px: Polygon,
    spec: KeyHolderSpec,
    *,
    density_g_cm3: float = 1.24,
) -> KeyHolderPlan:
    """Scale, clean, assemble and check a key holder layout.

    `silhouette_px` comes from `trace.trace_polygon`: pixel units, y already
    pointing up.
    """
    problems = spec.validate()
    if problems:
        raise DesignError("; ".join(problems))

    notes: list[str] = []
    warnings: list[str] = []
    spec = _fit_thickness(spec, notes)

    minx, miny, maxx, maxy = silhouette_px.bounds
    span = maxx - minx
    if span <= 0:
        raise DesignError("the traced outline has no width")
    scale = spec.width_mm / span

    # Scaling about the outline's own corner leaves that corner where it was, so
    # the translation that follows is by the untouched original offset. Getting
    # this pair wrong puts the model tens of millimetres off its own origin,
    # which is invisible in the renders and wrong in the slicer.
    scaled = affinity.scale(silhouette_px, xfact=scale, yfact=scale, origin=(minx, miny))
    scaled = affinity.translate(scaled, xoff=-minx, yoff=-miny)

    silhouette, clean_notes = _clean(scaled, spec)
    notes.extend(clean_notes)
    silhouette = _simplify(silhouette, spec.simplify_mm)

    minx, miny, maxx, maxy = silhouette.bounds
    rail_rect: Polygon | None = None
    rail_top_y: float | None = None
    if spec.rail and spec.rail_h_mm > 0:
        inset = min(spec.rail_inset_mm, (maxx - minx) / 2 - 10.0)
        inset = max(0.0, inset)
        rail_top = miny + spec.rail_overlap_mm
        rail_rect = _rounded_box(
            minx + inset,
            rail_top - spec.rail_h_mm,
            maxx - inset,
            rail_top,
            spec.rail_corner_r_mm,
        )
        rail_top_y = rail_top

    if rail_rect is not None:
        plate = unary_union([silhouette, rail_rect])
        plate, dropped = _connected_to(plate, rail_rect)
        if dropped > 0.005:
            warnings.append(
                f"{dropped:.0%} of the silhouette does not touch the rail and was "
                "dropped; it would have printed as a loose piece. Raise "
                "--rail-overlap to reach it, or add a --border."
            )
    else:
        plate = _largest(silhouette)
        loose = 1.0 - plate.area / max(silhouette.area, 1e-9)
        if loose > 0.005:
            warnings.append(
                f"{loose:.0%} of the silhouette is not joined to the largest "
                "piece and was dropped. With no rail there is nothing to join "
                "it to: add --border, or drop --no-rail."
            )

    plate = orient(_largest(plate), sign=1.0)
    # Normalise last, once the rail has pushed the outline below the silhouette's
    # own bottom edge: the finished plate sits with its lower-left corner on the
    # origin, so "y = 0" means the bottom of the part in the script, in the
    # renders and in the slicer alike.
    minx, miny = plate.bounds[0], plate.bounds[1]
    plate = affinity.translate(plate, xoff=-minx, yoff=-miny)
    if rail_rect is not None and rail_top_y is not None:
        rail_rect = affinity.translate(rail_rect, xoff=-minx, yoff=-miny)
        rail_top_y -= miny

    # The mount search runs on the plate as it will be built, holes included:
    # a keyhole that lands in the middle of a cut-out window is not a mount.
    mounts, mount_notes = _place_mounts(plate, spec, rail_top_y=rail_top_y)
    notes.extend(mount_notes)
    mount_mode = spec.mount if mounts else "none"

    hook_x, hook_base_y, hook_notes = _place_hooks(plate, spec, rail_rect, rail_top_y)
    notes.extend(hook_notes)

    interiors = [list(ring.coords) for ring in plate.interiors]
    holes: list[list[tuple[float, float]]] = []
    engrave: list[list[tuple[float, float]]] = []
    if spec.detail == "cut":
        holes = [_round_points(ring) for ring in interiors]
    elif spec.detail == "engrave":
        engrave = [_round_points(ring) for ring in interiors]
        if engrave:
            notes.append(
                f"{len(engrave)} interior opening(s) engraved "
                f"{spec.engrave_depth_mm:g} mm into the face rather than cut "
                "through, which keeps the plate stiff"
            )
    elif interiors:
        notes.append(f"{len(interiors)} interior opening(s) ignored")

    outer = _round_points(list(plate.exterior.coords))
    plate_area = plate.area if spec.detail == "cut" else Polygon(plate.exterior.coords).area

    minx, miny, maxx, maxy = plate.bounds
    size = (maxx - minx, maxy - miny)
    volume_mm3 = plate_area * spec.plaque_t_mm + len(hook_x) * _hook_volume(spec)
    mass_g = volume_mm3 / 1000.0 * density_g_cm3

    built = KeyHolderPlan(
        spec=spec,
        outline=outer,
        holes=holes,
        engrave=engrave,
        hook_x_mm=hook_x,
        hook_profile=[
            (round(z, 3), round(y, 3)) for z, y in hook_profile(spec)
        ],
        hook_base_y_mm=round(hook_base_y, 3),
        mounts=mounts,
        mount=mount_mode,
        plaque_t_mm=spec.plaque_t_mm,
        size_mm=(round(size[0], 2), round(size[1], 2)),
        scale_mm_per_px=scale,
        area_mm2=plate_area,
        estimated_mass_g=mass_g,
        notes=notes,
        warnings=warnings,
    )
    built.warnings.extend(_check_build_volume(built, spec))
    return built


def _fit_thickness(spec: KeyHolderSpec, notes: list[str]) -> KeyHolderSpec:
    """Thicken the plate if a keyhole cannot fit inside it.

    A keyhole is a sandwich: a retaining lip against the wall, a cavity for the
    screw head, and enough material in front that the head does not print
    through the visible face. Three numbers that must sum to less than the
    plate, which is exactly the kind of relationship a per-parameter range
    cannot express.
    """
    if spec.mount != "keyhole":
        return spec
    needed = round(spec.keyhole_lip_mm + spec.keyhole_head_h_mm + KEYHOLE_FRONT_WALL_MM, 3)
    if spec.plaque_t_mm >= needed:
        return spec
    if not spec.auto_thicken:
        raise DesignError(
            f"a keyhole needs {needed:.1f} mm of plate ({spec.keyhole_lip_mm:g} mm "
            f"lip + {spec.keyhole_head_h_mm:g} mm screw head + "
            f"{KEYHOLE_FRONT_WALL_MM:g} mm front wall) and this one is "
            f"{spec.plaque_t_mm:g} mm. Thicken it, or use --mount screw."
        )
    ceiling = PARAM_RANGES["plaque_t_mm"][1]
    if needed > ceiling:
        raise DesignError(
            f"a keyhole for a {spec.keyhole_head_d_mm:g} mm screw head needs "
            f"{needed:.1f} mm of plate, over the {ceiling:g} mm maximum. Use a "
            "smaller screw, or --mount screw."
        )
    notes.append(
        f"thickened the plate from {spec.plaque_t_mm:g} mm to {needed:.1f} mm so "
        "the keyhole has a lip, a head cavity and a front wall"
    )
    return replace(spec, plaque_t_mm=round(needed, 2))


def _place_hooks(
    plate: Polygon,
    spec: KeyHolderSpec,
    rail_rect: Polygon | None,
    rail_top_y: float | None,
) -> tuple[list[float], float, list[str]]:
    """Space the hooks along the rail, and refuse the ones with nothing behind them."""
    notes: list[str] = []
    if rail_rect is None or rail_top_y is None:
        return [], 0.0, notes

    minx, _, maxx, _ = rail_rect.bounds
    end_margin = max(6.0, spec.hook_w_mm)
    usable = (maxx - minx) - 2 * end_margin
    if usable <= spec.hook_w_mm:
        notes.append("the rail is too short for any hook")
        return [], 0.0, notes

    count = spec.hook_count or max(2, min(12, round(usable / HOOK_PITCH_TARGET_MM) + 1))
    if count < 1:
        return [], 0.0, notes
    if count > 1:
        pitch = usable / (count - 1)
        if pitch < spec.hook_w_mm + 8.0:
            count = max(2, round(usable // (spec.hook_w_mm + 8.0)) + 1)
            notes.append(
                f"reduced the hook count to {count}: any more and the bunches "
                "hanging on them would collide"
            )
    xs = (
        [minx + end_margin + usable / 2.0]
        if count == 1
        else [minx + end_margin + i * usable / (count - 1) for i in range(count)]
    )

    # The hook root has to have plate behind it for its whole width and height.
    base_y = rail_top_y - spec.rail_h_mm + 1.0
    kept: list[float] = []
    for x in xs:
        root = box(
            x - spec.hook_w_mm / 2.0,
            base_y,
            x + spec.hook_w_mm / 2.0,
            base_y + spec.hook_root_h_mm,
        )
        if plate.contains(root):
            kept.append(round(x, 3))
    if len(kept) < len(xs):
        notes.append(
            f"dropped {len(xs) - len(kept)} hook(s) that had no plate behind them"
        )
    if not kept:
        notes.append("no hook had a solid backing; the rail is narrower than it looks")
    return kept, base_y, notes


def _hook_volume(spec: KeyHolderSpec) -> float:
    """Shoelace area of the hook profile times its width."""
    points = hook_profile(spec)
    area = 0.0
    for i, (z0, y0) in enumerate(points):
        z1, y1 = points[(i + 1) % len(points)]
        area += z0 * y1 - z1 * y0
    return abs(area) / 2.0 * spec.hook_w_mm


def _check_build_volume(built: KeyHolderPlan, spec: KeyHolderSpec) -> list[str]:
    printer = spec.printer
    limits = printer.build_volume_mm
    width, height, depth = built.bounding_box_mm()
    problems = []
    if width > limits[0] or height > limits[1] or depth > limits[2]:
        usable = min(
            limits[0] / max(width, 1e-9),
            limits[1] / max(height, 1e-9),
            limits[2] / max(depth, 1e-9),
        )
        problems.append(
            f"{width:.0f} x {height:.0f} x {depth:.0f} mm does not fit "
            f"{printer.display_name} ({limits[0]:.0f} x {limits[1]:.0f} x "
            f"{limits[2]:.0f} mm). The widest that fits is about "
            f"{spec.width_mm * usable:.0f} mm."
        )
    return problems


def _round_points(coords) -> list[tuple[float, float]]:
    """Coordinates for the emitted script: 3 decimals, no repeated closing point.

    Three decimals is a micron, well under the kernel's own tolerance and under
    anything a printer can express -- and it is what keeps `source.py` a file a
    person can read and edit.
    """
    points = [(round(float(x), 3), round(float(y), 3)) for x, y in coords]
    if len(points) > 1 and points[0] == points[-1]:
        points = points[:-1]
    # Drop consecutive duplicates, which a rounded trace can produce and which
    # OCCT rejects as a zero-length edge.
    deduped: list[tuple[float, float]] = []
    for point in points:
        if not deduped or point != deduped[-1]:
            deduped.append(point)
    if len(deduped) > 2 and deduped[0] == deduped[-1]:
        deduped.pop()
    if len(deduped) < 3:
        raise DesignError("an outline collapsed to fewer than three distinct points")
    return deduped
