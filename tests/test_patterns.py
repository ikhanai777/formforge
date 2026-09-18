"""The pattern panel generator.

The property under test almost everywhere here is the same one: **a tile is a
window onto one global field**. Nearly every way this feature can fail is a way
that property can be broken -- a pattern that consults per-call state, a
normalisation computed per tile, a socket cut on the wrong side of an edge --
and each of those shows up as tiles that do not line up, which is not something
you find out until nine printed plates are on a table.

So the tests that matter most are the boring-looking ones: sampling a window
gives the same numbers as sampling the panel and slicing it, and two neighbours
agree along their shared edge to the last bit.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import trimesh
from shapely.geometry import box

from formforge.patterns import (
    FAMILIES,
    PATTERNS,
    JointSpec,
    MountSpec,
    PanelError,
    PanelSpec,
    PatternField,
    build_panel,
    get_pattern,
    joinery,
    labels,
    noise,
    plan_panel,
    resolve_params,
)
from formforge.patterns.field import Bounds
from formforge.patterns.library import build_field_fn
from formforge.patterns.surface import heightfield_solid
from formforge.patterns.tiling import row_label

BUILD_VOLUME = (220.0, 220.0, 250.0)

# Patterns whose output is driven by the seed. The analytic ones (a sine wave, a
# Julia set) are not, and asserting that they change with the seed would be
# asserting a bug.
SEEDED = {"dunes", "hills", "mountains", "canyon", "topographic", "flow", "voronoi", "truchet"}


def body_count(mesh: trimesh.Trimesh) -> int:
    """How many separate solids a mesh holds.

    trimesh's own `body_count` reaches for scipy; this asks networkx for the
    same connected components, so the test runs wherever the package's own
    dependencies do.
    """
    components = trimesh.graph.connected_components(
        mesh.face_adjacency, nodes=np.arange(len(mesh.faces)), engine="networkx"
    )
    return len(components)


def solid_volume(mesh: trimesh.Trimesh) -> float:
    """Volume of a boolean result, tolerating a degenerate one.

    Two tiles butted along a seam intersect in a zero-thickness sliver: real
    faces, no volume. trimesh divides by that volume to find the centre of
    mass, which warns; the number this wants is simply zero.
    """
    if len(mesh.faces) == 0:
        return 0.0
    with np.errstate(invalid="ignore", divide="ignore"):
        volume = float(mesh.volume)
    return volume if np.isfinite(volume) else 0.0


def make_field(pattern_id: str, width=300.0, height=200.0, seed=7, **params) -> PatternField:
    bounds = Bounds.sized(width, height)
    kernel, _, _ = build_field_fn(pattern_id, params or None, bounds, seed)
    return PatternField(fn=kernel, bounds=bounds).calibrate()


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pattern_id", sorted(PATTERNS))
def test_every_pattern_produces_usable_relief(pattern_id):
    """Finite, inside [0, 1], and actually varying.

    The last one is the real check. A pattern that returns a constant passes
    every mesh test there is and prints a flat plate.
    """
    field = make_field(pattern_id)
    heights = field.sample_grid(field.bounds, 120, 90)
    assert np.isfinite(heights).all(), f"{pattern_id} produced non-finite heights"
    assert heights.min() >= 0.0 and heights.max() <= 1.0
    assert heights.std() > 0.05, f"{pattern_id} is nearly flat (std {heights.std():.3f})"
    assert heights.max() - heights.min() > 0.5, f"{pattern_id} uses too little of the relief"


@pytest.mark.parametrize("pattern_id", sorted(PATTERNS))
def test_every_pattern_is_in_a_known_family(pattern_id):
    spec = get_pattern(pattern_id)
    assert spec.family in FAMILIES
    assert spec.description.strip()
    assert spec.suggested_relief_mm > 0


@pytest.mark.parametrize("pattern_id", sorted(PATTERNS))
def test_sampling_is_independent_of_the_window(pattern_id):
    """The seamlessness property, stated directly.

    A sub-window of the panel has to produce exactly what the same region of a
    full-panel sample produces. Any per-call state, any per-window
    normalisation, any RNG anywhere in a kernel breaks this -- and breaks it
    invisibly until the tiles are printed.
    """
    field = make_field(pattern_id)
    full = field.sample_grid(field.bounds, 161, 161)
    b = field.bounds
    half = Bounds(b.x0, b.y0, (b.x0 + b.x1) / 2, b.y1)
    window = field.sample_grid(half, 81, 161)
    np.testing.assert_array_equal(window, full[:81, :])


@pytest.mark.parametrize("pattern_id", sorted(PATTERNS))
def test_patterns_are_reproducible(pattern_id):
    a = make_field(pattern_id, seed=12).sample_grid(Bounds(-40, -30, 40, 30), 50, 40)
    b = make_field(pattern_id, seed=12).sample_grid(Bounds(-40, -30, 40, 30), 50, 40)
    np.testing.assert_array_equal(a, b)


@pytest.mark.parametrize("pattern_id", sorted(SEEDED))
def test_the_seed_changes_the_seeded_patterns(pattern_id):
    a = make_field(pattern_id, seed=1).sample_grid(Bounds(-40, -30, 40, 30), 50, 40)
    b = make_field(pattern_id, seed=2).sample_grid(Bounds(-40, -30, 40, 30), 50, 40)
    assert not np.allclose(a, b)


def test_noise_is_continuous_across_a_lattice_line():
    """A crease at every integer coordinate would be a ridge you can feel."""
    x = np.linspace(2.0 - 1e-4, 2.0 + 1e-4, 9)
    y = np.full_like(x, 0.37)
    values = noise.gradient_noise(x, y, seed=4)
    steps = np.abs(np.diff(values))
    assert steps.max() < 1e-3


def test_worley_reports_the_winning_cell():
    x = np.linspace(0.0, 6.0, 40)
    y = np.full_like(x, 1.5)
    f1, f2, value = noise.worley(x, y, seed=3)
    assert (f2 >= f1 - 1e-12).all()
    assert value.min() >= 0.0 and value.max() <= 1.0


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


def test_unknown_parameter_is_rejected_by_name():
    spec = get_pattern("waves")
    with pytest.raises(ValueError, match="has no parameter"):
        resolve_params(spec, {"wavelenght_mm": 30})


def test_a_close_misspelling_gets_a_suggestion():
    spec = get_pattern("waves")
    with pytest.raises(ValueError, match="did you mean wavelength_mm"):
        resolve_params(spec, {"wavelength_cm": 30})


def test_out_of_range_says_which_way_and_by_how_much():
    spec = get_pattern("waves")
    with pytest.raises(ValueError, match="below the minimum"):
        resolve_params(spec, {"wavelength_mm": 1})
    with pytest.raises(ValueError, match="above the maximum"):
        resolve_params(spec, {"wavelength_mm": 5000})


def test_choices_are_listed_when_one_is_wrong():
    spec = get_pattern("tpms")
    with pytest.raises(ValueError, match="gyroid"):
        resolve_params(spec, {"surface": "helicoid"})


def test_defaults_fill_everything_not_given():
    spec = get_pattern("dunes")
    values = resolve_params(spec, {"dune_mm": 200})
    assert values["dune_mm"] == 200
    assert set(values) == {p.name for p in spec.params}


# ---------------------------------------------------------------------------
# The field's finishing operations
# ---------------------------------------------------------------------------


def test_sampling_before_calibration_is_an_error():
    bounds = Bounds.sized(100, 100)
    kernel, _, _ = build_field_fn("waves", None, bounds, 0)
    field = PatternField(fn=kernel, bounds=bounds)
    with pytest.raises(RuntimeError, match="calibrate"):
        field.sample(np.zeros(3), np.zeros(3))


def test_a_constant_field_becomes_a_flat_plate_rather_than_a_crash():
    bounds = Bounds.sized(80, 60)
    field = PatternField(fn=lambda x, y: np.full_like(x, 4.2), bounds=bounds).calibrate()
    heights = field.sample_grid(bounds, 20, 20)
    assert np.isfinite(heights).all()


def test_nan_from_a_kernel_does_not_reach_the_mesh():
    bounds = Bounds.sized(40, 40)

    def broken(x, y):
        out = np.sin(x / 5.0)
        out[0] = np.nan
        return out

    field = PatternField(fn=broken, bounds=bounds).calibrate()
    heights = field.sample_grid(bounds, 12, 12)
    assert np.isfinite(heights).all()


def test_terracing_produces_discrete_levels():
    bounds = Bounds.sized(200, 200)
    kernel, _, _ = build_field_fn("hills", None, bounds, 3)
    field = PatternField(
        fn=kernel, bounds=bounds, terraces=6, terrace_sharpness=0.99
    ).calibrate()
    heights = field.sample_grid(bounds, 120, 120)
    # With a hard riser, nearly every sample sits on a tread; only the few that
    # land inside a riser are anywhere else. Measuring the fraction rather than
    # the count of distinct values is the honest test -- a smooth field crosses
    # every riser somewhere, so some samples are always in transit.
    treads = np.round(heights * 6) / 6
    on_a_tread = np.abs(heights - treads) < 1e-6
    assert on_a_tread.mean() > 0.95


def test_the_border_tapers_the_panel_edge_to_flat():
    bounds = Bounds.sized(200, 150)
    kernel, _, _ = build_field_fn("waves", None, bounds, 0)
    field = PatternField(fn=kernel, bounds=bounds, border_mm=20.0).calibrate()
    edge = field.sample(np.array([bounds.x0]), np.array([0.0]))
    assert edge[0] == pytest.approx(0.0, abs=1e-9)


def test_invert_swaps_crests_and_troughs():
    bounds = Bounds.sized(120, 120)
    kernel, _, _ = build_field_fn("waves", None, bounds, 0)
    normal = PatternField(fn=kernel, bounds=bounds).calibrate().sample_grid(bounds, 40, 40)
    flipped = (
        PatternField(fn=kernel, bounds=bounds, invert=True)
        .calibrate()
        .sample_grid(bounds, 40, 40)
    )
    np.testing.assert_allclose(normal + flipped, 1.0, atol=1e-9)


# ---------------------------------------------------------------------------
# The grid
# ---------------------------------------------------------------------------


def test_auto_grid_uses_the_fewest_tiles_that_fit():
    plan = plan_panel(600, 400, build_volume_mm=BUILD_VOLUME)
    assert plan.cols == 3 and plan.rows == 2
    assert plan.tile_w_mm == pytest.approx(200.0)
    assert plan.tile_h_mm == pytest.approx(200.0)
    assert plan.count == 6


def test_a_panel_that_fits_on_the_plate_is_one_tile():
    plan = plan_panel(150, 120, build_volume_mm=BUILD_VOLUME)
    assert plan.count == 1
    assert plan.tiles[0].label == "A1"
    assert not any(plan.tiles[0].seams.values())


def test_the_joint_is_counted_against_the_build_volume():
    """A 210 mm tile fits; the same tile with 10 mm of dovetail does not."""
    assert plan_panel(210, 200, build_volume_mm=BUILD_VOLUME).count == 1
    plan = plan_panel(210, 200, build_volume_mm=BUILD_VOLUME, joint_growth_mm=10.0)
    assert plan.count > 1


def test_an_impossible_explicit_grid_is_refused_with_the_numbers():
    with pytest.raises(ValueError, match="does not fit"):
        plan_panel(900, 600, build_volume_mm=BUILD_VOLUME, rows=1, cols=1)


def test_labels_read_the_way_the_wall_does():
    plan = plan_panel(600, 400, build_volume_mm=BUILD_VOLUME)
    top_left = plan.tile("A1")
    assert top_left.row == 0 and top_left.col == 0
    # Row A is the top of the panel, so it holds the highest Y.
    assert top_left.bounds.y1 == pytest.approx(plan.panel.y1)
    assert top_left.bounds.x0 == pytest.approx(plan.panel.x0)
    assert plan.tile("B3").row == 1 and plan.tile("B3").col == 2


def test_row_labels_continue_past_z():
    assert row_label(0) == "A"
    assert row_label(25) == "Z"
    assert row_label(26) == "AA"


def test_seams_and_neighbours_agree():
    plan = plan_panel(600, 400, build_volume_mm=BUILD_VOLUME)
    middle = plan.tile("A2")
    assert middle.seams == {"left": True, "right": True, "top": False, "bottom": True}
    assert middle.neighbours() == {"left": "A1", "right": "A3", "bottom": "B2"}


def test_tiles_cover_the_panel_exactly():
    plan = plan_panel(600, 400, build_volume_mm=BUILD_VOLUME)
    area = sum(t.bounds.width * t.bounds.height for t in plan.tiles)
    assert area == pytest.approx(600 * 400)


def test_adjacent_tiles_share_their_seam_exactly():
    """The whole feature in one assertion."""
    field = make_field("dunes", 600, 400, seed=5)
    plan = plan_panel(600, 400, build_volume_mm=BUILD_VOLUME)
    left, right = plan.tile("A1"), plan.tile("A2")
    assert left.bounds.x1 == right.bounds.x0
    ys = np.linspace(left.bounds.y0, left.bounds.y1, 401)
    from_left = field.sample(np.full_like(ys, left.bounds.x1), ys)
    from_right = field.sample(np.full_like(ys, right.bounds.x0), ys)
    np.testing.assert_array_equal(from_left, from_right)


# ---------------------------------------------------------------------------
# Meshing
# ---------------------------------------------------------------------------


def test_the_height_field_solid_is_watertight_and_right_way_out():
    field = make_field("waves", 120, 80)
    mesh, pitch = heightfield_solid(
        field, Bounds(-60, -40, 60, 40), base_mm=3.0, relief_mm=5.0, pitch_mm=1.0
    )
    assert mesh.is_watertight
    assert mesh.is_volume
    assert mesh.volume > 0
    assert pitch == pytest.approx(1.0, abs=0.05)
    lo, hi = mesh.bounds
    assert lo[2] == pytest.approx(0.0)
    assert hi[2] <= 3.0 + 5.0 + 1e-6
    assert (hi[0] - lo[0]) == pytest.approx(120.0)


def test_a_flat_field_gives_a_slab_of_exactly_the_right_volume():
    bounds = Bounds.sized(60, 40)
    field = PatternField(fn=lambda x, y: np.zeros_like(x), bounds=bounds).calibrate()
    mesh, _ = heightfield_solid(field, bounds, base_mm=2.5, relief_mm=0.0, pitch_mm=2.0)
    assert mesh.volume == pytest.approx(60 * 40 * 2.5, rel=1e-6)


def test_layer_snapping_quantises_the_relief():
    field = make_field("hills", 100, 100)
    bounds = Bounds(-50, -50, 50, 50)
    mesh, _ = heightfield_solid(
        field, bounds, base_mm=2.0, relief_mm=6.0, pitch_mm=2.0, layer_mm=0.2
    )
    z = mesh.vertices[:, 2]
    relief = z[z > 1e-9] - 2.0
    steps = relief[relief > 1e-9] / 0.2
    np.testing.assert_allclose(steps, np.round(steps), atol=1e-6)


def test_the_triangle_budget_is_respected():
    field = make_field("waves", 400, 400)
    bounds = Bounds(-200, -200, 200, 200)
    mesh, pitch = heightfield_solid(
        field, bounds, base_mm=3.0, relief_mm=4.0, pitch_mm=0.2, max_samples=20_000
    )
    assert len(mesh.faces) < 90_000
    assert pitch > 0.2


# ---------------------------------------------------------------------------
# Joinery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("style", ["dovetail", "puzzle"])
def test_in_plane_joints_mate_without_interference(style):
    """Tab and socket describe the same region, opened up by the clearance.

    The failure this catches is the one that looks fine in every render: a
    socket oriented outwards, cut out of the empty space beside the tile
    instead of out of the tile, leaving two male tabs colliding at the seam.
    """
    spec = JointSpec(style=style)
    plan = plan_panel(
        360,
        240,
        build_volume_mm=BUILD_VOLUME,
        rows=2,
        cols=2,
        joint_growth_mm=spec.tab_depth_mm,
    )
    shapes = {
        t.label: joinery.footprint(t.rect, t.seams, spec, polarity=t.polarity)
        for t in plan.tiles
    }
    for a, b in (("A1", "A2"), ("A1", "B1")):
        overlap = shapes[a].intersection(shapes[b])
        assert overlap.area < 1e-6, f"{a} and {b} collide by {overlap.area:.3f} mm^2"
        union = shapes[a].union(shapes[b])
        assert union.geom_type == "Polygon", f"{a} and {b} do not touch at all"


def test_the_female_socket_is_cut_out_of_the_tile():
    spec = JointSpec(style="dovetail")
    rect = (0.0, 0.0, 180.0, 120.0)
    seams = {"left": True, "right": False, "top": False, "bottom": False}
    shape = joinery.footprint(rect, seams, spec, polarity={"left": "female"})
    assert shape.area < 180.0 * 120.0
    assert shape.bounds == pytest.approx(rect)


def test_the_male_tab_grows_the_tile():
    spec = JointSpec(style="dovetail")
    rect = (0.0, 0.0, 180.0, 120.0)
    seams = {"left": False, "right": True, "top": False, "bottom": False}
    shape = joinery.footprint(rect, seams, spec, polarity={"right": "male"})
    assert shape.area > 180.0 * 120.0
    assert shape.bounds[2] == pytest.approx(180.0 + spec.tab_depth_mm)


def test_a_butt_joint_leaves_the_outline_alone():
    spec = JointSpec(style="butt")
    rect = (0.0, 0.0, 100.0, 80.0)
    seams = dict.fromkeys(joinery.EDGES, True)
    assert joinery.footprint(rect, seams, spec).equals(box(*rect))


def test_each_tile_gets_half_of_every_key_socket():
    """A key spans the seam, so the two halves have to add up to one key."""
    spec = JointSpec(style="key")
    plan = plan_panel(360, 200, build_volume_mm=BUILD_VOLUME, rows=1, cols=2)
    left, right = plan.tiles
    left_pockets = joinery.back_pockets(left.rect, left.seams, spec)
    right_pockets = joinery.back_pockets(right.rect, right.seams, spec)
    assert left_pockets and right_pockets
    whole = joinery.key_polygon(spec).buffer(spec.clearance_mm / 2.0, join_style=2).area
    total = sum(p.polygon.area for p in left_pockets) + sum(
        p.polygon.area for p in right_pockets
    )
    assert total == pytest.approx(whole * len(left_pockets), rel=0.02)


def test_keys_are_counted_once_per_seam():
    spec = JointSpec(style="key")
    plan = plan_panel(600, 400, build_volume_mm=BUILD_VOLUME)  # 3 x 2
    count = joinery.keys_per_panel(
        [t.rect for t in plan.tiles], [t.seams for t in plan.tiles], spec
    )
    # Seven interior seams of 200 mm; at the 110 mm default spacing each takes
    # two keys, and each seam is counted once rather than once per tile.
    assert count == 14


def test_a_butt_joint_needs_no_parts():
    spec = JointSpec(style="butt")
    plan = plan_panel(600, 400, build_volume_mm=BUILD_VOLUME)
    assert (
        joinery.keys_per_panel(
            [t.rect for t in plan.tiles], [t.seams for t in plan.tiles], spec
        )
        == 0
    )


def test_a_key_socket_deeper_than_the_base_is_refused():
    spec = JointSpec(style="key", key_depth_mm=3.0)
    problems = spec.preconditions(base_mm=2.0, min_floor_mm=1.0)
    assert problems and "Thicken the base" in problems[0]


def test_an_unknown_joint_style_lists_the_known_ones():
    with pytest.raises(ValueError, match="dovetail"):
        JointSpec(style="mortice")


def test_the_keyhole_traps_the_screw_head():
    """Two pockets: a shallow one over the whole slot, a deep one under the head."""
    pockets = joinery.mount_pockets((0.0, 0.0, 180.0, 140.0), MountSpec(style="keyhole"))
    assert len(pockets) == 2
    slot, head = pockets
    assert slot.depth_mm < head.depth_mm
    assert head.polygon.area < slot.polygon.area
    # The head opening is near the top of the tile, so the tile hangs from it.
    assert head.polygon.centroid.y > 100.0


def test_magnet_pockets_sit_inside_the_tile():
    pockets = joinery.mount_pockets((0.0, 0.0, 180.0, 140.0), MountSpec(style="magnets"))
    assert len(pockets) == 2
    for pocket in pockets:
        assert box(0.0, 0.0, 180.0, 140.0).contains(pocket.polygon)


def test_a_magnet_pocket_that_breaks_through_is_refused():
    problems = MountSpec(style="magnets", magnet_h_mm=3.0).preconditions(
        base_mm=3.0, min_floor_mm=1.0
    )
    assert problems and "magnet pocket" in problems[0]


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def test_a_label_renders_and_is_mirrored_for_the_back():
    front = labels.text_polygon("B2", height_mm=12.0, mirror=False)
    back = labels.text_polygon("B2", height_mm=12.0, mirror=True)
    assert front.area > 0
    assert back.area == pytest.approx(front.area, rel=1e-9)
    # Mirroring moves material across the centre line; the two are not the same.
    assert not front.equals(back)


def test_a_label_is_one_piece_per_glyph_not_a_pile_of_pixels():
    """Diagonal pixels have to merge, or the extrusion has self-touching rings."""
    glyph = labels.text_polygon("X", height_mm=14.0, mirror=False)
    assert glyph.is_valid
    assert glyph.geom_type == "Polygon"


def test_unknown_characters_are_skipped_rather_than_fatal():
    assert labels.text_polygon("A/B", height_mm=10.0) is not None
    assert labels.text_polygon("///", height_mm=10.0) is None


def test_the_arrow_points_up():
    arrow = labels.arrow_polygon(height_mm=10.0)
    ys = np.array(arrow.exterior.coords)[:, 1]
    assert ys.max() == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Whole panels
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def small_panel(tmp_path_factory) -> tuple[Path, object]:
    """One 2 x 2 keyed panel, built once and interrogated by several tests."""
    out = tmp_path_factory.mktemp("panel")
    spec = PanelSpec(
        pattern_id="waves",
        width_mm=240.0,
        height_mm=160.0,
        rows=2,
        cols=2,
        seed=4,
        base_mm=5.0,
        relief_mm=4.0,
        pitch_mm=2.0,
        joint=JointSpec(style="key"),
        mount=MountSpec(style="magnets"),
    )
    return out, build_panel(spec, out)


@pytest.mark.slow
def test_a_panel_writes_a_bundle_not_a_file(small_panel):
    out, result = small_panel
    assert (out / "assembly.json").exists()
    assert (out / "ASSEMBLY.md").exists()
    assert (out / "parts" / "key.stl").exists()
    assert len(result.tiles) == 4
    for tile in result.tiles:
        assert Path(tile.files["stl"]).exists()
        assert Path(tile.files["3mf"]).exists()


@pytest.mark.slow
def test_every_tile_is_a_printable_solid(small_panel):
    _, result = small_panel
    for tile in result.tiles:
        mesh = trimesh.load(tile.files["stl"])
        assert mesh.is_watertight, f"{tile.label} is not watertight"
        assert mesh.volume > 0
        # Sitting on the plate, back down.
        assert mesh.bounds[0][2] == pytest.approx(0.0, abs=1e-6)
        assert mesh.bounds[1][2] <= 5.0 + 4.0 + 1e-6


@pytest.mark.slow
def test_tiles_are_exported_at_the_origin_but_remember_where_they_belong(small_panel):
    _, result = small_panel
    for tile in result.tiles:
        mesh = trimesh.load(tile.files["stl"])
        assert mesh.bounds[0][0] == pytest.approx(0.0, abs=1e-6)
        assert mesh.bounds[0][1] == pytest.approx(0.0, abs=1e-6)
    positions = {t.label: t.position_mm for t in result.tiles}
    assert positions["A1"][1] > positions["B1"][1]


@pytest.mark.slow
def test_the_assembly_document_says_everything_needed_to_reprint(small_panel):
    out, _ = small_panel
    assembly = json.loads((out / "assembly.json").read_text())
    assert assembly["pattern"]["id"] == "waves"
    assert assembly["pattern"]["seed"] == 4
    assert set(assembly["pattern"]["parameters"]) == {
        p.name for p in get_pattern("waves").params
    }
    assert assembly["grid"]["rows"] == 2 and assembly["grid"]["cols"] == 2
    assert assembly["parts"]["key"]["count"] == 4
    assert len(assembly["tiles"]) == 4
    assert "A1" in assembly["grid"]["map"]

    markdown = (out / "ASSEMBLY.md").read_text()
    assert "formforge pattern waves" in markdown
    assert "back down" in markdown


@pytest.mark.slow
def test_the_recorded_command_carries_every_overridden_parameter(tmp_path):
    spec = PanelSpec(
        pattern_id="waves",
        width_mm=120.0,
        height_mm=100.0,
        params={"wavelength_mm": 30},
        seed=9,
        base_mm=3.0,
        relief_mm=2.0,
        pitch_mm=3.0,
        joint=JointSpec(style="butt"),
    )
    build_panel(spec, tmp_path)
    markdown = (tmp_path / "ASSEMBLY.md").read_text()
    assert "--set wavelength_mm=30" in markdown
    assert "--seed 9" in markdown


@pytest.mark.slow
def test_tile_labels_remove_material_from_the_back(tmp_path):
    def panel(label: bool, where: Path) -> float:
        spec = PanelSpec(
            pattern_id="hex_bumps",
            width_mm=120.0,
            height_mm=100.0,
            base_mm=3.0,
            relief_mm=3.0,
            pitch_mm=3.0,
            label_tiles=label,
        )
        return build_panel(spec, where).tiles[0].volume_mm3

    with_label = panel(True, tmp_path / "with")
    without = panel(False, tmp_path / "without")
    assert with_label < without


@pytest.mark.slow
def test_a_single_tile_panel_needs_no_joint_at_all(tmp_path):
    spec = PanelSpec(
        pattern_id="quasicrystal",
        width_mm=150.0,
        height_mm=150.0,
        base_mm=4.0,
        relief_mm=3.0,
        pitch_mm=2.0,
        joint=JointSpec(style="key"),
    )
    result = build_panel(spec, tmp_path)
    assert result.plan.count == 1
    assert result.parts == {}
    assert result.tiles[0].neighbours == {}


@pytest.mark.slow
@pytest.mark.parametrize("style", ["butt", "key", "bar", "dovetail", "puzzle"])
def test_every_joint_style_builds_solid_tiles(style, tmp_path):
    spec = PanelSpec(
        pattern_id="truchet",
        width_mm=260.0,
        height_mm=180.0,
        rows=2,
        cols=2,
        base_mm=5.0,
        relief_mm=3.0,
        pitch_mm=2.5,
        joint=JointSpec(style=style),
    )
    result = build_panel(spec, tmp_path)
    assert len(result.tiles) == 4
    for tile in result.tiles:
        mesh = trimesh.load(tile.files["stl"])
        assert mesh.is_watertight, f"{style}/{tile.label} is not watertight"
        assert body_count(mesh) == 1, f"{style}/{tile.label} came apart"


@pytest.mark.slow
@pytest.mark.parametrize("style", ["butt", "key", "dovetail", "puzzle"])
def test_the_printed_tiles_assemble_without_interference(style, tmp_path):
    """Put the tiles back where they came from and check nothing collides.

    The 2D footprint test proves the outlines mate. This proves the *solids*
    do, on the meshes that were actually exported, which is the closest thing
    to printing them. A union that comes back watertight with exactly the
    summed volume means the two tiles touch along the seam and overlap
    nowhere.
    """
    spec = PanelSpec(
        pattern_id="waves",
        width_mm=300.0,
        height_mm=180.0,
        rows=1,
        cols=2,
        base_mm=5.0,
        relief_mm=4.0,
        pitch_mm=2.5,
        joint=JointSpec(style=style),
        write_3mf=False,
    )
    result = build_panel(spec, tmp_path)

    placed = []
    for tile in result.tiles:
        mesh = trimesh.load(tile.files["stl"])
        mesh.apply_translation([tile.position_mm[0], tile.position_mm[1], 0.0])
        placed.append(mesh)

    overlap_volume = solid_volume(trimesh.boolean.intersection(placed, engine="manifold"))
    assert overlap_volume < 1e-3, f"{style} tiles interfere by {overlap_volume:.3f} mm^3"

    union = trimesh.boolean.union(placed, engine="manifold")
    assert union.is_watertight
    assert union.volume == pytest.approx(sum(m.volume for m in placed), rel=1e-6)


@pytest.mark.slow
def test_the_printed_key_drops_into_its_sockets(tmp_path):
    """The key is a separate print, so its fit is not guaranteed by construction."""
    from shapely import affinity

    from formforge.patterns.surface import prism

    joint = JointSpec(style="key")
    spec = PanelSpec(
        pattern_id="waves",
        width_mm=300.0,
        height_mm=180.0,
        rows=1,
        cols=2,
        base_mm=5.0,
        relief_mm=4.0,
        pitch_mm=2.5,
        joint=joint,
        write_3mf=False,
    )
    result = build_panel(spec, tmp_path)
    placed = []
    for tile in result.tiles:
        mesh = trimesh.load(tile.files["stl"])
        mesh.apply_translation([tile.position_mm[0], tile.position_mm[1], 0.0])
        placed.append(mesh)
    panel = trimesh.boolean.union(placed, engine="manifold")

    # One 180 mm seam takes two keys at the default spacing, a third of the way
    # along and two thirds along, on the seam line at x = 0.
    thickness = max(joint.key_depth_mm - 0.3, 0.8)
    for y in (-30.0, 30.0):
        key = prism(
            affinity.translate(joinery.key_polygon(joint), xoff=0.0, yoff=y),
            z0=0.0,
            z1=thickness,
        )
        volume = solid_volume(trimesh.boolean.intersection([panel, key], engine="manifold"))
        assert volume < 1e-3, f"the key at y={y} fouls the tile by {volume:.3f} mm^3"


@pytest.mark.slow
def test_the_one_piece_version_is_written_when_asked_for(tmp_path):
    spec = PanelSpec(
        pattern_id="spiral",
        width_mm=140.0,
        height_mm=140.0,
        base_mm=3.0,
        relief_mm=3.0,
        pitch_mm=2.0,
        joint=JointSpec(style="butt"),
        single=True,
    )
    result = build_panel(spec, tmp_path)
    assert result.single_path and Path(result.single_path).exists()
    mesh = trimesh.load(result.single_path)
    assert mesh.is_watertight
    assert mesh.bounds[1][0] - mesh.bounds[0][0] == pytest.approx(140.0)


def test_a_base_thinner_than_the_key_is_refused_before_any_geometry(tmp_path):
    spec = PanelSpec(
        pattern_id="waves",
        width_mm=300.0,
        height_mm=200.0,
        base_mm=2.0,
        joint=JointSpec(style="key", key_depth_mm=3.0),
    )
    with pytest.raises(PanelError, match="key socket"):
        build_panel(spec, tmp_path)
    assert not (tmp_path / "tiles").exists()


def test_a_panel_taller_than_the_build_volume_is_refused(tmp_path):
    spec = PanelSpec(
        pattern_id="waves",
        width_mm=200.0,
        height_mm=200.0,
        base_mm=4.0,
        relief_mm=300.0,
        joint=JointSpec(style="butt"),
    )
    with pytest.raises(PanelError, match="build height"):
        build_panel(spec, tmp_path)


def test_an_unknown_pattern_lists_the_known_ones(tmp_path):
    with pytest.raises(KeyError, match="dunes"):
        build_panel(PanelSpec(pattern_id="waffles", width_mm=100, height_mm=100), tmp_path)
