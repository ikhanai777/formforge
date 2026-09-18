"""How the tiles hold on to each other, and how the panel holds on to the wall.

The problem this solves is not "cut the panel up". Cutting is trivial -- the
field is continuous, so any window of it is a valid tile. The problem is that
nine loose slabs of PLA are not a wall panel, and the joint has to survive three
constraints at once:

1. **It has to be assemblable.** An in-plane dovetail locks beautifully across
   one axis and makes a grid impossible: a tile with dovetails on its right and
   its top edge has to slide in two directions at once to go in. That is why
   the default joint is a key on the back, which is fitted after the tiles are
   already laid out, and why `dovetail` here means dovetails on the vertical
   seams and keys on the horizontal ones.
2. **It has to print without supports.** Everything here is either a vertical
   wall or a flat floor in a back-down orientation. The one exception is the
   keyhole, which bridges 4.5 mm over its own slot -- well inside what any
   machine bridges cleanly.
3. **It has to fit.** Two printed surfaces at nominal dimensions do not go
   together; every mating pair here is opened up by `clearance_mm`, applied to
   the *socket* so the visible part keeps its drawn size. 0.25 mm suits a
   well-tuned 0.4 mm nozzle, 0.35 mm is the safer first try on an untuned one.

Everything is built as a shapely polygon and handed to the mesh layer, which
extrudes and subtracts it. Keeping the joinery two-dimensional is what makes it
checkable: an overlap between a magnet pocket and a key socket is an
intersection test, not a mesh interrogation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from shapely import affinity
from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union

__all__ = [
    "JOINT_STYLES",
    "MOUNT_STYLES",
    "BackPocket",
    "JointSpec",
    "MountSpec",
    "back_pockets",
    "footprint",
    "key_polygon",
    "keys_per_panel",
    "mount_pockets",
]

JOINT_STYLES = {
    "butt": "Plain edges. Glue or tape the tiles together; nothing to print but the tiles.",
    "key": (
        "Bowtie keys dropped into sockets on the back, spanning each seam. Works on "
        "both axes, assembles face-down in any order, and pulls the seam closed."
    ),
    "bar": (
        "Rectangular splines on the back. Aligns the tiles but does not resist "
        "being pulled apart."
    ),
    "dovetail": (
        "Interlocking dovetails on the vertical seams -- columns slide together -- "
        "with bowtie keys on the horizontal seams."
    ),
    "puzzle": (
        "Jigsaw tabs on every seam, pressed together in plane. No extra parts, but it "
        "needs the most clearance and the most force."
    ),
}

MOUNT_STYLES = {
    "none": "No mounting features. Adhesive strips on the back.",
    "magnets": "Round pockets on the back for disc magnets.",
    "keyhole": (
        "A keyhole slot on the back of each tile: hangs on a screw head, slides "
        "down to lock."
    ),
}

# Edge names, and the direction each one faces.
EDGES = ("left", "right", "bottom", "top")


@dataclass(frozen=True)
class JointSpec:
    """The seam joint, and the numbers that make it fit."""

    style: str = "key"
    clearance_mm: float = 0.25
    # Back keys
    key_length_mm: float = 34.0  # across the seam
    key_width_mm: float = 15.0  # along the seam, at the wide ends
    key_depth_mm: float = 3.0
    # In-plane tabs
    tab_depth_mm: float = 10.0
    tab_width_mm: float = 16.0
    # Nominal distance between joints along one seam.
    spacing_mm: float = 110.0

    def __post_init__(self) -> None:
        if self.style not in JOINT_STYLES:
            raise ValueError(
                f"unknown joint style {self.style!r}; "
                f"known styles: {', '.join(sorted(JOINT_STYLES))}"
            )
        if self.clearance_mm < 0:
            raise ValueError("clearance cannot be negative")

    @property
    def uses_back_keys(self) -> bool:
        return self.style in {"key", "bar", "dovetail"}

    @property
    def uses_in_plane_tabs(self) -> bool:
        return self.style in {"dovetail", "puzzle"}

    def key_edges(self) -> tuple[str, ...]:
        """Which seam edges get a back key."""
        if self.style in {"key", "bar"}:
            return EDGES
        if self.style == "dovetail":
            # The vertical seams are already dovetailed; keys go on the
            # horizontal ones, which is what lets the assembled columns be
            # stacked and locked without sliding anything sideways.
            return ("bottom", "top")
        return ()

    def tab_edges(self) -> tuple[str, ...]:
        """Which seam edges get an in-plane tab or socket."""
        if self.style == "dovetail":
            return ("left", "right")
        if self.style == "puzzle":
            return EDGES
        return ()

    def preconditions(self, *, base_mm: float, min_floor_mm: float) -> list[str]:
        """Relationships that must hold before anything is built.

        Checked here, against the numbers, rather than discovered later as "the
        tile fell into three pieces" -- which is what a key socket cut straight
        through a 2 mm back actually produces.
        """
        problems: list[str] = []
        if self.uses_back_keys:
            required = self.key_depth_mm + min_floor_mm
            if base_mm < required:
                problems.append(
                    f"base is {base_mm:.1f} mm but a {self.key_depth_mm:.1f} mm key socket "
                    f"needs at least {required:.1f} mm to leave a {min_floor_mm:.1f} mm floor "
                    f"under it. Thicken the base or shallow the key."
                )
            if self.key_width_mm >= self.key_length_mm:
                problems.append(
                    "the key is wider along the seam than it is long across it, so it "
                    "would not span the joint; reduce key_width_mm"
                )
        if self.uses_in_plane_tabs and self.tab_width_mm < 6.0:
            problems.append("in-plane tabs under 6 mm wide snap off; widen tab_width_mm")
        return problems


@dataclass(frozen=True)
class MountSpec:
    """How the panel attaches to the wall."""

    style: str = "none"
    magnet_d_mm: float = 10.0
    magnet_h_mm: float = 3.0
    magnets_per_tile: int = 2
    keyhole_head_d_mm: float = 9.0
    keyhole_slot_w_mm: float = 4.5
    keyhole_travel_mm: float = 14.0
    keyhole_head_depth_mm: float = 4.0
    keyhole_lip_depth_mm: float = 1.2

    def __post_init__(self) -> None:
        if self.style not in MOUNT_STYLES:
            raise ValueError(
                f"unknown mount style {self.style!r}; "
                f"known styles: {', '.join(sorted(MOUNT_STYLES))}"
            )

    def preconditions(self, *, base_mm: float, min_floor_mm: float) -> list[str]:
        problems: list[str] = []
        if self.style == "magnets":
            required = self.magnet_h_mm + 0.2 + min_floor_mm
            if base_mm < required:
                problems.append(
                    f"base is {base_mm:.1f} mm but a {self.magnet_h_mm:.1f} mm magnet pocket "
                    f"needs at least {required:.1f} mm to keep a floor over it"
                )
        if self.style == "keyhole":
            required = self.keyhole_head_depth_mm + min_floor_mm
            if base_mm < required:
                problems.append(
                    f"base is {base_mm:.1f} mm but the keyhole needs "
                    f"{self.keyhole_head_depth_mm:.1f} mm for the screw head plus a "
                    f"{min_floor_mm:.1f} mm floor -- {required:.1f} mm in total"
                )
            if self.keyhole_slot_w_mm >= self.keyhole_head_d_mm:
                problems.append(
                    "the keyhole slot is as wide as its head opening, so the screw head "
                    "would pull straight out; narrow keyhole_slot_w_mm"
                )
        return problems


@dataclass(frozen=True)
class BackPocket:
    """A pocket cut into the back of a tile, from z = 0 up to `depth_mm`."""

    polygon: Polygon
    depth_mm: float
    kind: str


def _joint_count(length: float, spacing: float) -> int:
    """How many joints fit along a seam of this length."""
    return max(1, round(length / max(spacing, 1e-6)))


def _joint_offsets(length: float, count: int) -> list[float]:
    """Evenly spaced positions along a seam, as offsets from its start.

    Both tiles either side of a seam compute this from the *same* seam length,
    so they agree without having to be told about each other.
    """
    return [length * (i + 1) / (count + 1) for i in range(count)]


def bowtie(length: float, width: float, waist: float = 0.5) -> Polygon:
    """A butterfly key: wide at both ends, pinched at the seam it crosses.

    The pinch is the point. A rectangular spline aligns two tiles and does
    nothing to stop them separating; a bowtie has to be pulled *through* its own
    waist to let go, so the seam is held closed by the key rather than by the
    glue.
    """
    half_l = length / 2.0
    half_w = width / 2.0
    half_waist = width * waist / 2.0
    return Polygon(
        [
            (-half_l, -half_w),
            (-half_l, half_w),
            (0.0, half_waist),
            (half_l, half_w),
            (half_l, -half_w),
            (0.0, -half_waist),
        ]
    )


def key_polygon(spec: JointSpec) -> Polygon:
    """The key part itself, at nominal size, lying along X."""
    if spec.style == "bar":
        return box(
            -spec.key_length_mm / 2,
            -spec.key_width_mm / 2,
            spec.key_length_mm / 2,
            spec.key_width_mm / 2,
        )
    return bowtie(spec.key_length_mm, spec.key_width_mm)


def _dovetail_tab(depth: float, width: float) -> Polygon:
    """A trapezoid growing wider with depth, centred on the origin, pointing +X."""
    flare = width * 0.32
    return Polygon(
        [
            (0.0, -width / 2.0),
            (depth, -width / 2.0 - flare),
            (depth, width / 2.0 + flare),
            (0.0, width / 2.0),
        ]
    )


def _puzzle_tab(depth: float, width: float) -> Polygon:
    """A jigsaw tab: a narrow neck and a round head, pointing +X.

    The head has to be wider than the neck or the joint does not hold; it also
    has to fit inside the depth it was given, which is why the radius is bounded
    by both. A head that reaches back to the edge is a half-disc, and a half-disc
    slides straight out.
    """
    head_r = min(width / 2.0, depth * 0.45)
    centre = depth - head_r
    neck_half = head_r * 0.6
    neck = box(-0.01, -neck_half, centre, neck_half)
    head = Point(centre, 0.0).buffer(head_r, quad_segs=24)
    return unary_union([neck, head])


def _oriented(shape: Polygon, edge: str, at: tuple[float, float], *, inward: bool) -> Polygon:
    """Place a +X-pointing shape against `edge`, pointing out of or into the tile.

    Both sides of a seam describe the *same* region of space: the male tile adds
    it outside its own edge, the female tile removes it from inside. So the
    female socket points the opposite way to the male tab on the same edge --
    get that backwards and the socket is cut out of thin air next to the tile,
    which is a hole in nothing and a joint that does not go together.
    """
    rotation = {"right": 0.0, "top": 90.0, "left": 180.0, "bottom": 270.0}[edge]
    if inward:
        rotation = (rotation + 180.0) % 360.0
    placed = affinity.rotate(shape, rotation, origin=(0.0, 0.0))
    return affinity.translate(placed, xoff=at[0], yoff=at[1])


def _edge_geometry(
    rect: tuple[float, float, float, float], edge: str
) -> tuple[float, tuple[float, float], str]:
    """Seam length, the seam's start point, and the axis it runs along."""
    x0, y0, x1, y1 = rect
    if edge == "left":
        return y1 - y0, (x0, y0), "y"
    if edge == "right":
        return y1 - y0, (x1, y0), "y"
    if edge == "bottom":
        return x1 - x0, (x0, y0), "x"
    return x1 - x0, (x0, y1), "x"


def footprint(
    rect: tuple[float, float, float, float],
    seams: dict[str, bool],
    spec: JointSpec,
    *,
    polarity: dict[str, str] | None = None,
) -> Polygon:
    """The tile outline, with in-plane tabs added and sockets cut.

    `polarity` says which side of each seam this tile is on. The convention is
    set by the tiling layer: a tile carries the tabs on its right and top edges
    and the sockets on its left and bottom, so every seam has exactly one of
    each and no tile has to be told about its neighbours.
    """
    outline = box(*rect)
    if not spec.uses_in_plane_tabs:
        return outline

    polarity = polarity or {}
    additions: list[Polygon] = []
    removals: list[Polygon] = []
    shape_for = _dovetail_tab if spec.style == "dovetail" else _puzzle_tab

    for edge in spec.tab_edges():
        if not seams.get(edge):
            continue
        length, start, axis = _edge_geometry(rect, edge)
        count = _joint_count(length, spec.spacing_mm)
        tab = shape_for(spec.tab_depth_mm, spec.tab_width_mm)
        for offset in _joint_offsets(length, count):
            at = (start[0], start[1] + offset) if axis == "y" else (start[0] + offset, start[1])
            male = polarity.get(edge) == "male"
            placed = _oriented(tab, edge, at, inward=not male)
            if male:
                additions.append(placed)
            else:
                # The socket is the tab opened up by the clearance, mitred so
                # the dovetail keeps its straight flanks instead of being
                # rounded off by the buffer.
                removals.append(placed.buffer(spec.clearance_mm, join_style=2, mitre_limit=8.0))

    if additions:
        outline = unary_union([outline, *additions])
    for cut in removals:
        outline = outline.difference(cut)
    if outline.geom_type == "MultiPolygon":
        # A socket that reached across the tile would do this. It is a design
        # error rather than a mesh error, and it is worth saying so in those terms.
        raise ValueError(
            "the seam sockets cut the tile into separate pieces; reduce tab_depth_mm "
            "or increase the tile size"
        )
    return outline


def back_pockets(
    rect: tuple[float, float, float, float],
    seams: dict[str, bool],
    spec: JointSpec,
) -> list[BackPocket]:
    """Key sockets on the back of one tile: the half of each key that is its own."""
    if not spec.uses_back_keys:
        return []

    pockets: list[BackPocket] = []
    nominal = key_polygon(spec)
    socket = nominal.buffer(spec.clearance_mm / 2.0, join_style=2, mitre_limit=8.0)
    tile = box(*rect)

    for edge in spec.key_edges():
        if not seams.get(edge):
            continue
        length, start, axis = _edge_geometry(rect, edge)
        count = _joint_count(length, spec.spacing_mm)
        for offset in _joint_offsets(length, count):
            if axis == "y":
                at = (start[0], start[1] + offset)
                placed = affinity.translate(socket, xoff=at[0], yoff=at[1])
            else:
                at = (start[0] + offset, start[1])
                placed = affinity.translate(
                    affinity.rotate(socket, 90.0, origin=(0.0, 0.0)), xoff=at[0], yoff=at[1]
                )
            half = placed.intersection(tile)
            if half.is_empty or half.area <= 0:
                continue
            for piece in getattr(half, "geoms", [half]):
                if isinstance(piece, Polygon) and piece.area > 0:
                    pockets.append(BackPocket(piece, spec.key_depth_mm, "key"))
    return pockets


def keys_per_panel(
    tile_rects: list[tuple[float, float, float, float]],
    seam_map: list[dict[str, bool]],
    spec: JointSpec,
) -> int:
    """How many keys to print for the whole panel.

    Counted from the tiles that *own* each seam -- the right and top edges --
    so a seam shared by two tiles is counted once.
    """
    if not spec.uses_back_keys:
        return 0
    owned = {"right", "top"} & set(spec.key_edges())
    total = 0
    for rect, seams in zip(tile_rects, seam_map, strict=True):
        for edge in owned:
            if not seams.get(edge):
                continue
            length, _, _ = _edge_geometry(rect, edge)
            total += _joint_count(length, spec.spacing_mm)
    return total


def mount_pockets(
    rect: tuple[float, float, float, float],
    spec: MountSpec,
    *,
    clearance_mm: float = 0.15,
) -> list[BackPocket]:
    """Wall-mounting features cut into the back of a tile."""
    if spec.style == "none":
        return []
    x0, y0, x1, y1 = rect
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    width = x1 - x0
    height = y1 - y0

    if spec.style == "magnets":
        radius = (spec.magnet_d_mm + clearance_mm) / 2.0
        inset = max(radius + 8.0, min(width, height) * 0.18)
        count = max(1, int(spec.magnets_per_tile))
        if count >= 4:
            centres = [
                (x0 + inset, y0 + inset),
                (x1 - inset, y0 + inset),
                (x0 + inset, y1 - inset),
                (x1 - inset, y1 - inset),
            ]
        elif count == 2:
            centres = [(x0 + inset, cy), (x1 - inset, cy)]
        else:
            centres = [(cx, cy)]
        return [
            BackPocket(
                Point(px, py).buffer(radius, quad_segs=32),
                spec.magnet_h_mm + 0.2,
                "magnet",
            )
            for px, py in centres
        ]

    # Keyhole. Two stacked pockets: a shallow one the shape of the whole
    # keyhole, so the screw shank has somewhere to travel, and a deep one under
    # the round end only, so the head is trapped behind the lip once the tile
    # has slid down. Printed back-down, the lip is a 4.5 mm bridge.
    head_r = (spec.keyhole_head_d_mm + clearance_mm) / 2.0
    slot_w = spec.keyhole_slot_w_mm + clearance_mm
    top_y = min(y1 - max(head_r + 10.0, height * 0.12), y1 - head_r - 4.0)
    head_centre = (cx, top_y)
    head = Point(*head_centre).buffer(head_r, quad_segs=32)
    slot = box(cx - slot_w / 2.0, top_y - spec.keyhole_travel_mm, cx + slot_w / 2.0, top_y)
    return [
        BackPocket(unary_union([head, slot]), spec.keyhole_lip_depth_mm, "keyhole_slot"),
        BackPocket(head, spec.keyhole_head_depth_mm, "keyhole_head"),
    ]


def overlaps(pockets: list[BackPocket]) -> list[tuple[str, str]]:
    """Pairs of back features that collide -- reported, not silently merged."""
    found: list[tuple[str, str]] = []
    for i, a in enumerate(pockets):
        for b in pockets[i + 1 :]:
            if a.kind.startswith("keyhole") and b.kind.startswith("keyhole"):
                continue  # the keyhole's two pockets are meant to be concentric
            if a.kind == "label" and b.kind == "label":
                continue  # the glyphs of one label are separate by construction
            if a.polygon.intersects(b.polygon) and a.polygon.intersection(b.polygon).area > 0.5:
                found.append((a.kind, b.kind))
    return found


def seam_length_total(
    tile_rects: list[tuple[float, float, float, float]],
    seam_map: list[dict[str, bool]],
) -> float:
    """Total length of interior seam in the panel, in mm. Used for reporting."""
    total = 0.0
    for rect, seams in zip(tile_rects, seam_map, strict=True):
        for edge in ("right", "top"):
            if seams.get(edge):
                length, _, _ = _edge_geometry(rect, edge)
                total += length
    return math.fsum([total])
