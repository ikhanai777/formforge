"""Build a whole panel: every tile, every part, and the map that explains them.

This is the layer that turns "a 600 x 400 mm dune field" into a directory
somebody can print from. What comes out is a bundle in the same spirit as the
one `formforge generate` produces -- files, not a file -- because a set of six
STLs with no record of which is which and no count of how many keys to print is
not a deliverable, it is homework.

    tiles/A1.stl ...      one per tile, each moved to the origin, back down
    parts/key.stl         the joint part, if the joint needs one
    assembly.json         positions, seams, neighbours, counts, every parameter
    ASSEMBLY.md           the same thing for a human, with the grid drawn out
    panel.stl             the whole thing in one piece, when asked for

The ordering of operations inside a tile matters and is not arbitrary: the
footprint is clipped *before* the back pockets are cut, so a key socket that
would have landed on a dovetail flank cuts nothing rather than cutting a
notch out of the flank.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import trimesh

from ..dfm import DEFAULT_PROFILE_ID, get_profile, limits_for
from ..validation.invariants import DENSITIES
from . import joinery, labels
from .export import write_3mf, write_stl
from .field import Bounds, PatternField
from .joinery import BackPocket, JointSpec, MountSpec
from .library import PatternSpec, build_field_fn
from .surface import (
    heightfield_solid,
    intersect_prism,
    prism,
    subtract,
    triangle_budget_pitch,
)
from .tiling import TilePlan, TileSpec, plan_panel

__all__ = ["PanelError", "PanelResult", "PanelSpec", "TileOutput", "build_panel"]

# Depth of the recessed grid reference and arrow on the back of each tile.
LABEL_DEPTH_MM = 0.6
LABEL_CAP_MM = 12.0

Progress = Callable[[str, str], None]


class PanelError(RuntimeError):
    """The panel cannot be built as specified, and the message says why."""


@dataclass(frozen=True)
class TileOutput:
    """One printed piece."""

    label: str
    row: int
    col: int
    files: dict[str, str]
    size_mm: tuple[float, float, float]
    position_mm: tuple[float, float]
    triangles: int
    volume_mm3: float
    mass_g: float
    neighbours: dict[str, str]
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "row": self.row,
            "col": self.col,
            "files": self.files,
            "size_mm": [round(v, 2) for v in self.size_mm],
            "panel_position_mm": [round(v, 2) for v in self.position_mm],
            "triangles": self.triangles,
            "volume_mm3": round(self.volume_mm3, 1),
            "mass_g": round(self.mass_g, 1),
            "neighbours": self.neighbours,
            "warnings": list(self.warnings),
        }


@dataclass
class PanelSpec:
    """Everything that decides what gets built. Recorded verbatim in the bundle."""

    pattern_id: str
    width_mm: float
    height_mm: float
    params: dict[str, Any] = dataclass_field(default_factory=dict)
    seed: int = 0
    relief_mm: float | None = None
    base_mm: float = 3.0
    rows: int | None = None
    cols: int | None = None
    max_tile_mm: float | None = None
    joint: JointSpec = dataclass_field(default_factory=JointSpec)
    mount: MountSpec = dataclass_field(default_factory=MountSpec)
    profile_id: str = DEFAULT_PROFILE_ID
    material: str = "PLA"
    pitch_mm: float | None = None
    triangle_budget: int = 400_000
    snap_layers: bool = False
    invert: bool = False
    contrast: float = 1.0
    terraces: int = 0
    terrace_sharpness: float = 0.75
    border_mm: float = 0.0
    label_tiles: bool = True
    write_3mf: bool = True
    single: bool = False


@dataclass
class PanelResult:
    """What got built."""

    directory: Path
    spec: PanelSpec
    pattern: PatternSpec
    resolved_params: dict[str, Any]
    plan: TilePlan
    tiles: list[TileOutput]
    parts: dict[str, dict[str, Any]]
    relief_mm: float
    pitch_mm: float
    assembly: dict[str, Any]
    warnings: list[str] = dataclass_field(default_factory=list)
    single_path: str | None = None

    @property
    def total_mass_g(self) -> float:
        return sum(t.mass_g for t in self.tiles)

    @property
    def total_triangles(self) -> int:
        return sum(t.triangles for t in self.tiles)


def build_panel(
    spec: PanelSpec, out_dir: str | Path, *, progress: Progress | None = None
) -> PanelResult:
    """Build every tile of a panel and write the bundle."""
    say = progress or (lambda step, message: None)

    profile = get_profile(spec.profile_id)
    limits = limits_for(profile, spec.material)
    bounds = Bounds.sized(spec.width_mm, spec.height_mm)

    kernel, resolved, pattern = build_field_fn(spec.pattern_id, spec.params, bounds, spec.seed)
    relief_mm = spec.relief_mm if spec.relief_mm is not None else pattern.suggested_relief_mm
    if relief_mm < 0:
        raise PanelError("relief depth cannot be negative")

    growth = spec.joint.tab_depth_mm if spec.joint.uses_in_plane_tabs else 0.0
    plan = plan_panel(
        spec.width_mm,
        spec.height_mm,
        build_volume_mm=profile.build_volume_mm,
        rows=spec.rows,
        cols=spec.cols,
        max_tile_mm=spec.max_tile_mm,
        joint_growth_mm=growth,
    )
    _check_preconditions(spec, profile, limits, relief_mm, jointed=plan.count > 1)

    field = PatternField(
        fn=kernel,
        bounds=bounds,
        invert=spec.invert,
        contrast=spec.contrast,
        terraces=spec.terraces,
        terrace_sharpness=spec.terrace_sharpness,
        border_mm=spec.border_mm,
    )
    say(
        "field",
        f"{pattern.display_name}: calibrating over "
        f"{spec.width_mm:.0f} x {spec.height_mm:.0f} mm",
    )
    field.calibrate()
    say(
        "plan",
        f"{plan.rows} x {plan.cols} = {plan.count} tile(s), "
        f"{plan.tile_w_mm:.0f} x {plan.tile_h_mm:.0f} mm each",
    )

    directory = Path(out_dir)
    (directory / "tiles").mkdir(parents=True, exist_ok=True)

    pitch = _resolve_pitch(spec, profile, plan)
    layer = profile.layer_mm if spec.snap_layers else None
    warnings: list[str] = []

    tiles: list[TileOutput] = []
    assembled: list[trimesh.Trimesh] = []
    for tile_spec in plan.tiles:
        say("tile", f"{tile_spec.label}: building")
        mesh, tile_warnings = _build_tile(
            tile_spec,
            field=field,
            spec=spec,
            relief_mm=relief_mm,
            pitch_mm=pitch,
            layer_mm=layer,
        )
        assembled.append(mesh.copy())
        placed = mesh.bounds[0]
        mesh.apply_translation([-placed[0], -placed[1], -placed[2]])
        files = _export(mesh, directory / "tiles", tile_spec.label, with_3mf=spec.write_3mf)
        size = tuple(float(v) for v in (mesh.bounds[1] - mesh.bounds[0]))
        density = DENSITIES.get(spec.material.upper(), 1.24)
        tiles.append(
            TileOutput(
                label=tile_spec.label,
                row=tile_spec.row,
                col=tile_spec.col,
                files=files,
                size_mm=size,  # type: ignore[arg-type]
                position_mm=(tile_spec.bounds.x0, tile_spec.bounds.y0),
                triangles=len(mesh.faces),
                volume_mm3=float(mesh.volume),
                mass_g=float(mesh.volume) / 1000.0 * density,
                neighbours=tile_spec.neighbours(),
                warnings=tuple(tile_warnings),
            )
        )
        warnings.extend(f"{tile_spec.label}: {w}" for w in tile_warnings)

    parts = _build_parts(spec, plan, directory, say)

    single_path: str | None = None
    if spec.single:
        say("single", "building the one-piece version")
        single_path = _build_single(spec, field, bounds, relief_mm, pitch, layer, directory)

    assembly = _assembly_document(
        spec, pattern, resolved, plan, tiles, parts, relief_mm, pitch, profile, warnings
    )
    (directory / "assembly.json").write_text(json.dumps(assembly, indent=2) + "\n")
    (directory / "ASSEMBLY.md").write_text(_assembly_markdown(assembly, plan, spec, pattern))

    # The assembled panel, for the preview renderer and for anyone who wants to
    # check the fit in a slicer before committing 40 hours of print time.
    if len(assembled) > 1:
        combined = trimesh.util.concatenate(assembled)
        combined.export(str(directory / "assembled_preview.stl"))

    say("done", f"{len(tiles)} tile(s), {sum(t.triangles for t in tiles)} triangles")
    return PanelResult(
        directory=directory,
        spec=spec,
        pattern=pattern,
        resolved_params=resolved,
        plan=plan,
        tiles=tiles,
        parts=parts,
        relief_mm=relief_mm,
        pitch_mm=pitch,
        assembly=assembly,
        warnings=warnings,
        single_path=single_path,
    )


# --------------------------------------------------------------------------
# Preconditions
# --------------------------------------------------------------------------


def _check_preconditions(
    spec: PanelSpec, profile, limits, relief_mm: float, *, jointed: bool
) -> None:
    """Everything checkable from the numbers alone, checked before any geometry.

    Same division as the template registry draws: a relationship between two
    parameters has nowhere to live in a per-parameter range check, and finding
    out about it afterwards produces "the mesh fell apart" instead of "those two
    numbers cannot both be right".
    """
    problems: list[str] = []
    if spec.base_mm < limits.min_floor_mm:
        problems.append(
            f"a {spec.base_mm:.1f} mm base is under the {limits.min_floor_mm:.1f} mm "
            f"minimum for {profile.display_name}: it will be translucent and it will curl"
        )
    if jointed:
        # A panel that came out as a single tile has no seams, so its joint is
        # never built and its numbers are nobody's business.
        problems += spec.joint.preconditions(
            base_mm=spec.base_mm, min_floor_mm=limits.min_floor_mm
        )
    problems += spec.mount.preconditions(base_mm=spec.base_mm, min_floor_mm=limits.min_floor_mm)

    total_z = spec.base_mm + relief_mm
    if total_z > profile.build_volume_mm[2]:
        problems.append(
            f"base plus relief is {total_z:.0f} mm, taller than the "
            f"{profile.build_volume_mm[2]:.0f} mm build height"
        )
    if problems:
        raise PanelError(
            "this panel cannot be built as specified:\n  - " + "\n  - ".join(problems)
        )


def _resolve_pitch(spec: PanelSpec, profile, plan: TilePlan) -> float:
    """Sample spacing, defaulted from the nozzle and capped by the triangle budget.

    Defaulting to twice the nozzle width rather than to the nozzle width is the
    honest choice: a 0.4 mm nozzle lays a 0.4 mm road, so detail finer than
    about 0.8 mm cannot survive being printed, and sampling it only quadruples
    the file size. `--resolution` overrides for anyone who disagrees.
    """
    requested = spec.pitch_mm if spec.pitch_mm else max(profile.nozzle_mm * 2.0, 0.5)
    tile_bounds = Bounds(0.0, 0.0, plan.tile_w_mm, plan.tile_h_mm)
    return triangle_budget_pitch(tile_bounds, requested, spec.triangle_budget)


# --------------------------------------------------------------------------
# One tile
# --------------------------------------------------------------------------


def _build_tile(
    tile: TileSpec,
    *,
    field: PatternField,
    spec: PanelSpec,
    relief_mm: float,
    pitch_mm: float,
    layer_mm: float | None,
) -> tuple[trimesh.Trimesh, list[str]]:
    warnings: list[str] = []

    outline = joinery.footprint(tile.rect, tile.seams, spec.joint, polarity=tile.polarity)
    # Sample over the footprint's own bounding box, which is wider than the tile
    # wherever a tab sticks out. The pattern continues across the tab -- it is
    # sampled from the same global field -- so the tab carries the neighbour's
    # surface and the seam does not show a step where the joint is.
    minx, miny, maxx, maxy = outline.bounds
    sample_bounds = Bounds(minx, miny, maxx, maxy)

    mesh, _ = heightfield_solid(
        field,
        sample_bounds,
        base_mm=spec.base_mm,
        relief_mm=relief_mm,
        pitch_mm=pitch_mm,
        layer_mm=layer_mm,
        max_samples=max(spec.triangle_budget // 4, 64),
    )
    bbox_area = sample_bounds.width * sample_bounds.height
    if spec.joint.uses_in_plane_tabs or outline.area < bbox_area - 1e-6:
        # A plain rectangular tile is already exactly its footprint, so it skips
        # the boolean entirely -- which is most tiles, of most panels.
        mesh = intersect_prism(mesh, outline)

    pockets: list[BackPocket] = []
    pockets += joinery.back_pockets(tile.rect, tile.seams, spec.joint)
    pockets += joinery.mount_pockets(tile.rect, spec.mount)
    if spec.label_tiles:
        pockets += _label_pockets(tile, outline)
    for kind_a, kind_b in joinery.overlaps(pockets):
        warnings.append(
            f"the {kind_a} recess and the {kind_b} recess overlap on the back; "
            f"they will merge into one cavity"
        )

    cutters = [prism(p.polygon, z0=-1.0, z1=p.depth_mm) for p in pockets if p.polygon.area > 0]
    mesh = subtract(mesh, cutters, "cutting the back features")
    return mesh, warnings


def _label_pockets(tile: TileSpec, outline) -> list[BackPocket]:
    """The grid reference and the up-arrow, recessed into the back.

    Returned as pockets rather than as finished cutters so they go through the
    same collision check as the joint and mount recesses. A label cut across a
    magnet pocket is not a structural problem -- the deeper pocket simply wins
    -- but it is an unreadable label, which is the one job the label has.
    """
    centroid = outline.centroid
    cap = min(LABEL_CAP_MM, max(6.0, min(tile.bounds.width, tile.bounds.height) * 0.12))
    gap = cap * 0.8
    text = labels.text_polygon(
        tile.label, height_mm=cap, center=(centroid.x, centroid.y - gap * 0.5)
    )
    arrow = labels.arrow_polygon(height_mm=cap, center=(centroid.x, centroid.y + gap * 0.7))

    pockets: list[BackPocket] = []
    for shape in (text, arrow):
        if shape is None or shape.is_empty:
            continue
        # Never let a label escape the tile: near a small tile's edge the text
        # would otherwise cut a notch out of the side wall.
        clipped = shape.intersection(outline.buffer(-2.0))
        if clipped.is_empty:
            continue
        for piece in getattr(clipped, "geoms", [clipped]):
            if piece.area > 0:
                pockets.append(BackPocket(piece, LABEL_DEPTH_MM, "label"))
    return pockets


# --------------------------------------------------------------------------
# Parts and the one-piece version
# --------------------------------------------------------------------------


def _build_parts(
    spec: PanelSpec, plan: TilePlan, directory: Path, say: Progress
) -> dict[str, dict[str, Any]]:
    """Loose parts the joint needs, and how many of each to print."""
    if not spec.joint.uses_back_keys:
        return {}
    rects = [t.rect for t in plan.tiles]
    seam_maps = [t.seams for t in plan.tiles]
    count = joinery.keys_per_panel(rects, seam_maps, spec.joint)
    if count == 0:
        return {}

    # The key is 0.3 mm thinner than its socket so it sits below the back face.
    # A key standing proud holds the whole panel off the wall on four points.
    thickness = max(spec.joint.key_depth_mm - 0.3, 0.8)
    key = prism(joinery.key_polygon(spec.joint), z0=0.0, z1=thickness)
    (directory / "parts").mkdir(parents=True, exist_ok=True)
    path = directory / "parts" / "key.stl"
    key.export(str(path))
    say(
        "parts",
        f"key: print {count}, {spec.joint.key_length_mm:.0f} x "
        f"{spec.joint.key_width_mm:.0f} x {thickness:.1f} mm",
    )
    return {
        "key": {
            "file": str(path),
            "count": count,
            "size_mm": [spec.joint.key_length_mm, spec.joint.key_width_mm, round(thickness, 2)],
            "note": (
                "Print a couple of spares. Keys are the cheapest part here and the "
                "one most likely to be a hair too tight."
            ),
        }
    }


def _build_single(
    spec: PanelSpec,
    field: PatternField,
    bounds: Bounds,
    relief_mm: float,
    pitch_mm: float,
    layer_mm: float | None,
    directory: Path,
) -> str:
    """The whole panel as one solid, for when it does fit on the plate."""
    mesh, _ = heightfield_solid(
        field,
        bounds,
        base_mm=spec.base_mm,
        relief_mm=relief_mm,
        pitch_mm=pitch_mm,
        layer_mm=layer_mm,
        max_samples=spec.triangle_budget * 4,
    )
    pockets = joinery.mount_pockets((bounds.x0, bounds.y0, bounds.x1, bounds.y1), spec.mount)
    mesh = subtract(
        mesh,
        [prism(p.polygon, z0=-1.0, z1=p.depth_mm) for p in pockets],
        "the one-piece back features",
    )
    mesh.apply_translation([-bounds.x0, -bounds.y0, 0.0])
    path = directory / "panel.stl"
    mesh.export(str(path))
    return str(path)


def _export(
    mesh: trimesh.Trimesh, directory: Path, name: str, *, with_3mf: bool
) -> dict[str, str]:
    """Write a tile.

    3MF first, as everywhere else in FormForge: it declares its units, and the
    "my panel printed 25.4 times too big" failure has exactly one cause.
    """
    directory.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    if with_3mf:
        files["3mf"] = write_3mf(mesh, directory / f"{name}.3mf", name=name)
    files["stl"] = write_stl(mesh, directory / f"{name}.stl")
    return files


# --------------------------------------------------------------------------
# The assembly document
# --------------------------------------------------------------------------


def _assembly_document(
    spec: PanelSpec,
    pattern: PatternSpec,
    resolved: dict[str, Any],
    plan: TilePlan,
    tiles: list[TileOutput],
    parts: dict[str, dict[str, Any]],
    relief_mm: float,
    pitch_mm: float,
    profile,
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "pattern": {
            "id": pattern.id,
            "display_name": pattern.display_name,
            "family": pattern.family,
            "parameters": resolved,
            "seed": spec.seed,
            "print_note": pattern.print_note,
        },
        "panel": {
            "width_mm": spec.width_mm,
            "height_mm": spec.height_mm,
            "base_mm": spec.base_mm,
            "relief_mm": relief_mm,
            "total_thickness_mm": round(spec.base_mm + relief_mm, 2),
            "sample_pitch_mm": round(pitch_mm, 3),
            "invert": spec.invert,
            "contrast": spec.contrast,
            "terraces": spec.terraces,
            "border_mm": spec.border_mm,
            "snap_layers": spec.snap_layers,
        },
        "grid": {
            "rows": plan.rows,
            "cols": plan.cols,
            "tile_w_mm": round(plan.tile_w_mm, 2),
            "tile_h_mm": round(plan.tile_h_mm, 2),
            "tile_footprint_mm": [round(v, 2) for v in plan.footprint_mm],
            "map": plan.ascii_map(),
        },
        "joint": {
            "style": spec.joint.style,
            "description": joinery.JOINT_STYLES[spec.joint.style],
            "clearance_mm": spec.joint.clearance_mm,
            "key_size_mm": [
                spec.joint.key_length_mm,
                spec.joint.key_width_mm,
                spec.joint.key_depth_mm,
            ],
            "tab_size_mm": [spec.joint.tab_depth_mm, spec.joint.tab_width_mm],
            "spacing_mm": spec.joint.spacing_mm,
        },
        "mount": {
            "style": spec.mount.style,
            "description": joinery.MOUNT_STYLES[spec.mount.style],
        },
        "printer": {
            "profile": profile.id,
            "display_name": profile.display_name,
            "nozzle_mm": profile.nozzle_mm,
            "layer_mm": profile.layer_mm,
            "build_volume_mm": list(profile.build_volume_mm),
            "material": spec.material,
        },
        "tiles": [t.as_dict() for t in tiles],
        "parts": parts,
        "totals": {
            "tiles": len(tiles),
            "triangles": sum(t.triangles for t in tiles),
            "volume_mm3": round(sum(t.volume_mm3 for t in tiles), 1),
            "mass_g": round(sum(t.mass_g for t in tiles), 1),
        },
        "warnings": warnings,
    }


def _assembly_markdown(
    assembly: dict, plan: TilePlan, spec: PanelSpec, pattern: PatternSpec
) -> str:
    joint = assembly["joint"]
    mount = assembly["mount"]
    totals = assembly["totals"]
    parts = assembly["parts"]

    lines = [
        f"# {pattern.display_name} panel",
        "",
        f"{spec.width_mm:.0f} x {spec.height_mm:.0f} mm, "
        f"{assembly['panel']['total_thickness_mm']:.1f} mm thick "
        f"({spec.base_mm:.1f} mm base + {assembly['panel']['relief_mm']:.1f} mm relief), "
        f"in {totals['tiles']} piece(s).",
        "",
        "## The grid",
        "",
        "Seen from the front, hanging on the wall:",
        "",
        "```",
        plan.ascii_map(),
        "```",
        "",
        "Each tile has its reference and an up-arrow recessed into the back, so a "
        "face-down tile can be placed without guessing."
        if spec.label_tiles
        else "Tile labels are switched off; keep the files in order.",
        "",
        "## Printing",
        "",
        f"- Printer profile: {assembly['printer']['display_name']}, {spec.material}.",
        "- Print every tile **back down, relief up**. No supports: every surface is "
        "either a wall or an upward-facing slope.",
        f"- Tile footprint: {plan.footprint_mm[0]:.0f} x {plan.footprint_mm[1]:.0f} mm "
        f"on a {assembly['printer']['build_volume_mm'][0]:.0f} x "
        f"{assembly['printer']['build_volume_mm'][1]:.0f} mm plate.",
        f"- Material if printed solid: {totals['mass_g']:.0f} g. At a typical 15% "
        f"infill expect roughly a third of that; the base and the relief skin are "
        f"what actually cost.",
    ]
    if pattern.print_note:
        lines.append(f"- {pattern.print_note}")
    lines += [
        "",
        "## Joining",
        "",
        f"**{joint['style']}** -- {joint['description']}",
        "",
        f"Clearance is {joint['clearance_mm']:.2f} mm. If the first joint is tight, "
        "reprint with a larger clearance rather than forcing it; if it is loose, a "
        "drop of glue in the socket is the fix.",
    ]
    if parts:
        lines.append("")
        for name, part in parts.items():
            size = part["size_mm"]
            lines.append(
                f"- Print **{part['count']} x {name}** "
                f"({size[0]:.0f} x {size[1]:.0f} x {size[2]:.1f} mm) from `parts/{name}.stl`. "
                f"{part['note']}"
            )
    lines += [
        "",
        _joining_steps(spec.joint.style),
        "",
        "## Mounting",
        "",
        f"**{mount['style']}** -- {mount['description']}",
        "",
        "## Reprinting one tile",
        "",
        "Everything here is deterministic. The same pattern, size, seed and parameters "
        "rebuild the identical panel, so a cracked tile is one command away:",
        "",
        "```",
        _reproduce_command(spec),
        "```",
        "",
        "Every parameter that went into this panel is in `assembly.json`.",
        "",
    ]
    return "\n".join(lines)


def _joining_steps(style: str) -> str:
    """How this particular joint actually goes together.

    Worth spelling out per style rather than writing one sentence for all of
    them: a dovetailed panel that is laid out flat and pressed together does
    not assemble at all, because its tiles have to be slid past each other
    along the seam before anything else can happen.
    """
    if style == "butt":
        return (
            "Butt the tiles together face down and run glue or seam tape along each "
            "joint. Nothing holds the seam but the adhesive, so work on a flat surface "
            "and weight the panel until it cures."
        )
    if style == "puzzle":
        return (
            "Press the tiles together in plane, face down on a flat surface. The jigsaw "
            "heads need a firm push past their necks. If a tab will not seat, reprint "
            "with more clearance rather than forcing it -- a head that snaps off costs "
            "the whole tile."
        )
    if style == "dovetail":
        return (
            "Assemble one row at a time: its tiles interlock along the vertical seams, "
            "so they slide together along the seam rather than pressing straight in. "
            "Then lay the assembled rows out face down and drop a key into each socket "
            "pair across the horizontal seams."
        )
    return (
        "Lay the tiles face down in the grid above, drop a key into each socket pair "
        "across a seam, then glue the keys in if the panel will be handled. Any order "
        "works -- nothing has to be slid into anything."
    )


def _reproduce_command(spec: PanelSpec) -> str:
    parts = [
        "formforge pattern",
        spec.pattern_id,
        f"--size {spec.width_mm:g}x{spec.height_mm:g}",
        f"--seed {spec.seed}",
        f"--base {spec.base_mm:g}",
    ]
    if spec.relief_mm is not None:
        parts.append(f"--relief {spec.relief_mm:g}")
    parts.append(f"--joint {spec.joint.style}")
    if spec.mount.style != "none":
        parts.append(f"--mount {spec.mount.style}")
    parts.append(f"--profile {spec.profile_id}")
    for name, value in sorted(spec.params.items()):
        parts.append(f"--set {name}={value}")
    return " ".join(parts)
