"""The image-to-key-holder path, from pixels to a printable solid.

The images here are drawn in numpy rather than committed as fixtures, so every
case says in code what shape it is testing and why: a disc with a hole is the
hole-detection case, a barbell is the thin-neck case, a blob in the top half is
the y-flip case. A binary PNG in a fixtures directory would say none of that.

Only the last class runs the CAD kernel.
"""

from __future__ import annotations

import ast
import io
from itertools import pairwise

import numpy as np
import pytest
from shapely.geometry import Polygon, box

from formforge import cli, security
from formforge.binding import declared_constants
from formforge.keyholder import (
    DETAIL_MODES,
    MOUNT_MODES,
    DesignError,
    ImageError,
    KeyHolderSpec,
    emit,
    plan_from_image,
    read_mask,
    trace_polygon,
)
from formforge.keyholder.design import (
    MOUNT_MARGIN_MM,
    hook_profile,
    mount_footprint,
)
from formforge.keyholder.image import (
    denoise,
    largest_component,
    otsu_threshold,
    smooth,
)
from formforge.keyholder.pipeline import (
    _adjust_for_report,
    param_schema,
)
from formforge.keyholder.trace import marching_squares

PIL = pytest.importorskip("PIL.Image", reason="the tracer needs Pillow")


# ---------------------------------------------------------------------------
# Images, drawn rather than committed
# ---------------------------------------------------------------------------


def _png(array: np.ndarray) -> bytes:
    """Encode an HxWx3 or HxWx4 uint8 array as PNG bytes."""
    mode = "RGBA" if array.shape[2] == 4 else "RGB"
    buffer = io.BytesIO()
    PIL.fromarray(array, mode=mode).save(buffer, format="PNG")
    return buffer.getvalue()


def _canvas(width: int = 240, height: int = 200, colour=(255, 255, 255)) -> np.ndarray:
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    canvas[:, :] = colour
    return canvas


def _disc(canvas: np.ndarray, cx: int, cy: int, r: int, colour=(20, 20, 20)) -> np.ndarray:
    ys, xs = np.mgrid[0 : canvas.shape[0], 0 : canvas.shape[1]]
    canvas[(xs - cx) ** 2 + (ys - cy) ** 2 <= r * r] = colour
    return canvas


def _rect(canvas: np.ndarray, x0, y0, x1, y1, colour=(20, 20, 20)) -> np.ndarray:
    canvas[y0:y1, x0:x1] = colour
    return canvas


@pytest.fixture
def blob_png() -> bytes:
    """A dark disc on white: the simplest thing anyone uploads."""
    return _png(_disc(_canvas(), 120, 100, 70))


@pytest.fixture
def ring_png() -> bytes:
    """A disc with a hole, for the interior-opening path."""
    canvas = _disc(_canvas(), 120, 100, 80)
    return _png(_disc(canvas, 120, 100, 30, colour=(255, 255, 255)))


@pytest.fixture
def wide_blob_png() -> bytes:
    """A wide rounded shape, so a rail full of hooks has something to sit under."""
    canvas = _rect(_canvas(320, 200), 20, 30, 300, 170)
    return _png(canvas)


class TestMaskExtraction:
    def test_otsu_lands_in_the_valley_of_a_bimodal_field(self):
        """Between the two clusters, not hard against one of them.

        Every cut between 0 and 1 separates this field equally well, so the
        arg-max is a tie. Taking the first of it would put the threshold a
        thousandth above the background, where one stray pixel becomes
        foreground.
        """
        values = np.concatenate([np.zeros(500), np.ones(500)])
        assert 0.2 < otsu_threshold(values) < 0.8

    def test_otsu_finds_an_asymmetric_split(self):
        values = np.concatenate([np.full(800, 0.1), np.full(200, 0.9)])
        assert 0.15 < otsu_threshold(values) < 0.85

    def test_a_dark_subject_on_white_is_the_foreground(self, blob_png):
        mask = read_mask(blob_png)
        assert mask.info.source == "colour-distance"
        assert not mask.info.inverted
        # A r=70 disc in a 240x200 frame is about a quarter of it.
        assert 0.15 < mask.info.foreground_fraction < 0.35

    def test_a_light_subject_on_a_dark_ground_is_still_the_foreground(self):
        canvas = _canvas(colour=(10, 90, 10))
        image = _png(_disc(canvas, 120, 100, 70, colour=(240, 240, 230)))
        mask = read_mask(image)
        assert 0.15 < mask.info.foreground_fraction < 0.35

    def test_alpha_is_believed_when_it_says_something(self):
        canvas = np.zeros((200, 240, 4), dtype=np.uint8)
        canvas[:, :, :3] = 200
        ys, xs = np.mgrid[0:200, 0:240]
        canvas[(xs - 120) ** 2 + (ys - 100) ** 2 <= 70 * 70, 3] = 255
        mask = read_mask(_png(canvas))
        assert mask.info.source == "alpha"

    def test_an_opaque_alpha_channel_is_ignored(self, blob_png):
        rgb = np.asarray(PIL.open(io.BytesIO(blob_png)).convert("RGB"))
        rgba = np.dstack([rgb, np.full((200, 240), 255)]).astype(np.uint8)
        assert read_mask(_png(rgba)).info.source == "colour-distance"

    def test_a_selection_covering_the_frame_is_read_as_background(self):
        """The failure this prevents is a key holder shaped like the wall.

        A bright frame around a dark picture makes the border colour
        unrepresentative, so the colour-distance pass selects nearly everything.
        Anything that large is the background, whatever the arithmetic says.
        """
        canvas = _canvas(colour=(10, 10, 10))
        canvas[:6, :] = canvas[-6:, :] = (250, 250, 250)
        canvas[:, :6] = canvas[:, -6:] = (250, 250, 250)
        mask = read_mask(_png(canvas))
        assert mask.info.inverted
        assert any("inverted" in note for note in mask.info.notes)

    def test_invert_can_be_forced(self, blob_png):
        assert read_mask(blob_png, invert=True).info.foreground_fraction > 0.6

    def test_an_empty_image_is_refused_with_an_actionable_message(self):
        with pytest.raises(ImageError) as exc:
            read_mask(_png(_canvas()))
        assert "threshold" in str(exc.value)

    def test_a_tiny_image_is_refused(self):
        with pytest.raises(ImageError):
            read_mask(_png(_canvas(4, 4)))

    def test_the_largest_region_wins(self, blob_png):
        canvas = _disc(_canvas(), 60, 100, 50)
        image = _png(_disc(canvas, 210, 30, 8))
        mask = read_mask(image)
        assert any("separate regions" in note for note in mask.info.notes)

    def test_speckle_does_not_become_geometry(self):
        canvas = _disc(_canvas(), 120, 100, 60)
        rng = np.random.default_rng(0)
        noise = rng.random(canvas.shape[:2]) < 0.02
        canvas[noise] = (20, 20, 20)
        clean = denoise(np.asarray(PIL.open(io.BytesIO(_png(canvas))).convert("L")) < 128)
        # The disc survives; the pepper does not.
        _, count, _ = largest_component(clean)
        assert count <= 3

    def test_enclosed_regions_are_found(self, ring_png):
        mask = read_mask(ring_png)
        assert any("openings in the subject" in note for note in mask.info.notes)


class TestTracing:
    def test_marching_squares_finds_one_ring_around_a_square(self):
        field = np.zeros((40, 40))
        field[10:30, 10:30] = 1.0
        rings = marching_squares(field)
        assert len(rings) == 1
        # The contour runs through the half-way point of the boundary cells, so
        # a 20-pixel block encloses 20 x 20 units, not 19 x 19: sub-pixel
        # interpolation is the whole reason this is not a pixel-edge walk.
        assert Polygon(rings[0]).area == pytest.approx(400.0, abs=1.0)

    def test_a_ring_becomes_a_polygon_with_a_hole(self, ring_png):
        polygon, stats = trace_polygon(smooth(read_mask(ring_png).array))
        assert stats.holes_kept == 1
        assert len(polygon.interiors) == 1

    def test_tiny_openings_are_ignored(self):
        canvas = _disc(_canvas(), 120, 100, 80)
        image = _png(_disc(canvas, 120, 100, 3, colour=(255, 255, 255)))
        polygon, _ = trace_polygon(smooth(read_mask(image).array))
        assert len(polygon.interiors) == 0

    def test_the_outline_is_y_up(self):
        """An image with its subject at the top must trace to a shape at the top.

        Rows count downwards in an image and upwards in every CAD kernel. Get
        this wrong and the key holder is built upside down, which no numeric
        check anywhere else would notice.
        """
        canvas = _canvas()
        _rect(canvas, 40, 10, 200, 60)  # near the top of the picture
        mask = read_mask(_png(canvas))
        polygon, _ = trace_polygon(smooth(mask.array))
        assert polygon.centroid.y > mask.array.shape[0] / 2

    def test_simplification_removes_the_pixel_staircase(self, blob_png):
        field = smooth(read_mask(blob_png).array)
        rough, _ = trace_polygon(field, simplify_px=0.0)
        smoothed, _ = trace_polygon(field, simplify_px=0.8)
        assert len(smoothed.exterior.coords) < len(rough.exterior.coords)
        assert smoothed.area == pytest.approx(rough.area, rel=0.02)


class TestSpec:
    def test_a_default_spec_is_valid(self):
        assert KeyHolderSpec().validate() == []

    def test_a_value_outside_its_range_is_refused(self):
        problems = KeyHolderSpec(width_mm=1000.0).validate()
        assert problems and "width_mm" in problems[0]

    def test_an_unknown_mode_is_refused(self):
        assert KeyHolderSpec(detail="emboss").validate()
        assert KeyHolderSpec(mount="glue").validate()

    def test_a_rail_too_short_for_its_hooks_is_refused_before_anything_is_built(self):
        problems = KeyHolderSpec(rail_h_mm=8.0, hook_root_h_mm=15.0).validate()
        assert problems and "rail_h_mm" in problems[0]

    def test_a_lip_taller_than_the_hook_projects_is_refused(self):
        problems = KeyHolderSpec(hook_out_mm=10.0, hook_lip_mm=12.0).validate()
        assert problems and "45 degrees" in problems[0]

    def test_a_feature_size_under_two_perimeters_is_refused(self):
        """The floor moves with the nozzle: 1 mm is fine at 0.4, not at 0.6."""
        assert KeyHolderSpec(min_feature_mm=1.0).validate() == []
        problems = KeyHolderSpec(min_feature_mm=1.0, profile_id="generic_fdm_0.6").validate()
        assert problems and "perimeters" in problems[0]


class TestHookProfile:
    """The hook is the only part of this design that cannot be fixed later.

    It prints in a direction its own shape has to be self-supporting in, and no
    support material can be removed from inside a hook, so the profile carries
    the whole no-supports claim.
    """

    def test_every_rising_edge_is_45_degrees_or_shallower(self):
        for spec in (
            KeyHolderSpec(),
            KeyHolderSpec(hook_out_mm=40.0, hook_lip_mm=20.0, hook_root_h_mm=30.0),
            KeyHolderSpec(hook_out_mm=9.0, hook_lip_mm=3.0, hook_root_h_mm=7.0, rail_h_mm=10.0),
        ):
            points = hook_profile(spec)
            closed = points + points[:1]
            for (z0, y0), (z1, y1) in pairwise(closed):
                rise = abs(y1 - y0)
                run = abs(z1 - z0)
                if z1 > z0 and rise > 1e-9:
                    assert rise <= run + 1e-6, f"{spec.hook_out_mm} mm hook overhangs"

    def test_the_lip_stands_above_the_arm(self):
        spec = KeyHolderSpec()
        points = hook_profile(spec)
        assert max(y for _, y in points) == pytest.approx(
            spec.hook_root_h_mm + spec.hook_lip_mm
        )

    def test_the_root_is_embedded_in_the_plate(self):
        assert min(z for z, _ in hook_profile(KeyHolderSpec())) < 0


class TestLayout:
    @pytest.fixture
    def wide_plan(self, wide_blob_png):
        built, _, _ = plan_from_image(wide_blob_png, KeyHolderSpec(width_mm=200.0))
        return built

    def test_the_plate_is_the_requested_width(self, wide_plan):
        assert wide_plan.size_mm[0] == pytest.approx(200.0, abs=0.5)

    def test_the_plate_sits_on_the_origin(self, wide_plan):
        xs = [x for x, _ in wide_plan.outline]
        ys = [y for _, y in wide_plan.outline]
        assert min(xs) == pytest.approx(0.0, abs=0.01)
        assert min(ys) == pytest.approx(0.0, abs=0.01)

    def test_the_rail_adds_height_below_the_silhouette(self, wide_blob_png):
        spec = KeyHolderSpec(width_mm=200.0)
        with_rail, _, _ = plan_from_image(wide_blob_png, spec)
        without, _, _ = plan_from_image(
            wide_blob_png, KeyHolderSpec(width_mm=200.0, rail=False, hook_count=0)
        )
        assert with_rail.size_mm[1] > without.size_mm[1]
        assert not without.hook_x_mm

    def test_hooks_are_evenly_spaced_and_backed_by_the_plate(self, wide_plan):
        assert len(wide_plan.hook_x_mm) >= 3
        gaps = np.diff(wide_plan.hook_x_mm)
        assert np.allclose(gaps, gaps[0], atol=0.01)
        plate = Polygon(wide_plan.outline)
        for x in wide_plan.hook_x_mm:
            root = box(
                x - wide_plan.spec.hook_w_mm / 2,
                wide_plan.hook_base_y_mm,
                x + wide_plan.spec.hook_w_mm / 2,
                wide_plan.hook_base_y_mm + wide_plan.spec.hook_root_h_mm,
            )
            assert plate.contains(root)

    def test_an_explicit_hook_count_is_honoured(self, wide_blob_png):
        built, _, _ = plan_from_image(
            wide_blob_png, KeyHolderSpec(width_mm=200.0, hook_count=3)
        )
        assert len(built.hook_x_mm) == 3

    def test_hooks_that_would_collide_are_thinned_out(self, wide_blob_png):
        built, _, _ = plan_from_image(
            wide_blob_png, KeyHolderSpec(width_mm=100.0, hook_count=12)
        )
        assert len(built.hook_x_mm) < 12
        assert any("collide" in note for note in built.notes)

    def test_two_fixings_land_at_the_same_height_inside_the_material(self, wide_plan):
        assert len(wide_plan.mounts) == 2
        left, right = wide_plan.mounts
        assert left.y_mm == pytest.approx(right.y_mm)
        assert right.x_mm - left.x_mm > 30.0
        plate = Polygon(wide_plan.outline, wide_plan.holes)
        for mount in wide_plan.mounts:
            footprint = mount_footprint(wide_plan.spec, mount.x_mm, mount.y_mm)
            assert plate.contains(footprint.buffer(MOUNT_MARGIN_MM * 0.99))

    def test_no_mount_is_asked_for_no_mount_is_cut(self, wide_blob_png):
        built, _, _ = plan_from_image(
            wide_blob_png, KeyHolderSpec(width_mm=200.0, mount="none")
        )
        assert built.mounts == []
        assert built.mount == "none"

    def test_a_thin_plate_is_thickened_to_fit_a_keyhole(self, wide_blob_png):
        built, _, _ = plan_from_image(
            wide_blob_png, KeyHolderSpec(width_mm=200.0, plaque_t_mm=4.0)
        )
        assert built.plaque_t_mm > 4.0
        assert any("thickened" in note for note in built.notes)

    def test_a_thin_plate_is_refused_when_thickening_is_not_allowed(self, wide_blob_png):
        with pytest.raises(DesignError) as exc:
            plan_from_image(
                wide_blob_png,
                KeyHolderSpec(width_mm=200.0, plaque_t_mm=4.0, auto_thicken=False),
            )
        assert "--mount screw" in str(exc.value)

    def test_a_screw_mount_needs_no_thickening(self, wide_blob_png):
        built, _, _ = plan_from_image(
            wide_blob_png, KeyHolderSpec(width_mm=200.0, plaque_t_mm=4.0, mount="screw")
        )
        assert built.plaque_t_mm == 4.0

    @staticmethod
    def _barbell() -> bytes:
        """Two discs joined by a neck 2 mm wide at the width these are planned at."""
        canvas = _canvas(400, 200)
        _disc(canvas, 90, 100, 60)
        _disc(canvas, 310, 100, 60)
        _rect(canvas, 150, 98, 250, 102)
        return _png(canvas)

    def test_a_neck_too_thin_to_print_is_removed(self):
        """With no rail to rejoin them, only one lobe can survive.

        The minimum-feature pass is a real guarantee rather than a nudge: at
        2.4 mm the 2 mm neck is deleted, the barbell falls into two pieces, and
        a key holder has to be one piece.
        """
        built, _, _ = plan_from_image(
            self._barbell(),
            KeyHolderSpec(width_mm=200.0, rail=False, hook_count=0, mount="none"),
        )
        assert built.size_mm[0] < 120.0

    def test_the_rail_rejoins_what_the_neck_no_longer_holds(self):
        built, _, _ = plan_from_image(self._barbell(), KeyHolderSpec(width_mm=200.0))
        assert built.size_mm[0] == pytest.approx(200.0, abs=1.0)
        assert Polygon(built.outline).is_valid

    def test_a_neck_thick_enough_to_print_is_kept(self):
        built, _, _ = plan_from_image(
            self._barbell(),
            KeyHolderSpec(
                width_mm=200.0, min_feature_mm=1.0, rail=False, hook_count=0, mount="none"
            ),
        )
        assert built.size_mm[0] == pytest.approx(200.0, abs=1.0)

    def test_a_shape_thinner_than_the_minimum_everywhere_is_refused(self):
        canvas = _rect(_canvas(400, 200), 20, 99, 380, 101)
        with pytest.raises(DesignError) as exc:
            plan_from_image(_png(canvas), KeyHolderSpec(width_mm=80.0))
        assert "min-feature" in str(exc.value) or "thinner" in str(exc.value)

    def test_detail_modes_choose_between_cutting_and_engraving(self, ring_png):
        engraved, _, _ = plan_from_image(ring_png, KeyHolderSpec(detail="engrave"))
        cut, _, _ = plan_from_image(ring_png, KeyHolderSpec(detail="cut"))
        ignored, _, _ = plan_from_image(ring_png, KeyHolderSpec(detail="ignore"))
        assert engraved.engrave and not engraved.holes
        assert cut.holes and not cut.engrave
        assert not ignored.holes and not ignored.engrave

    def test_something_too_big_for_the_printer_says_what_fits(self, wide_blob_png):
        built, _, _ = plan_from_image(
            wide_blob_png,
            KeyHolderSpec(width_mm=290.0, profile_id="prusa_mk4_0.4"),
        )
        assert built.warnings
        assert "widest that fits" in built.warnings[0]

    def test_the_plan_reports_what_it_decided(self, wide_plan):
        payload = wide_plan.as_dict()
        assert payload["hooks"] == len(wide_plan.hook_x_mm)
        assert payload["mount"] == "keyhole"
        assert payload["size_mm"][2] == pytest.approx(
            wide_plan.plaque_t_mm + wide_plan.spec.hook_out_mm
        )


@pytest.fixture(scope="module")
def emitted_script() -> str:
    canvas = _rect(_canvas(320, 200), 20, 30, 300, 170)
    built, _, _ = plan_from_image(_png(canvas), KeyHolderSpec(width_mm=200.0))
    return emit(built, source_name="test.png")


class TestEmittedScript:
    @pytest.fixture
    def script(self, emitted_script) -> str:
        return emitted_script

    def test_it_is_valid_python(self, script):
        ast.parse(script)

    def test_it_passes_the_static_gate(self, script):
        result = security.scan(script, enforce_named_constants=False)
        assert result.ok, result.report()

    def test_it_imports_nothing_but_build123d(self, script):
        assert security.scan(script).imports == {"build123d"}

    def test_every_dimension_is_a_named_constant(self, script):
        constants = declared_constants(script)
        for name in ("PLAQUE_T_MM", "HOOK_W_MM", "KEYHOLE_LIP_MM", "SCREW_D_MM"):
            assert name in constants, name
            assert isinstance(constants[name], float)

    def test_it_produces_a_result(self, script):
        assert script.rstrip().endswith("result = key_holder.part")

    def test_it_says_how_to_print_it(self, script):
        assert "no supports" in script
        assert "back face on the bed" in script

    def test_a_hostile_filename_cannot_break_the_script(self):
        """The one string in the file that FormForge did not write.

        A filename with a triple quote in it would close the header docstring
        early, and the result would be a syntax error the static gate rejects --
        an obscure way to be told a file had an odd name.
        """
        canvas = _rect(_canvas(320, 200), 20, 30, 300, 170)
        built, _, _ = plan_from_image(_png(canvas), KeyHolderSpec(width_mm=200.0))
        script = emit(built, source_name='evil""" \n import os #.png')
        ast.parse(script)
        assert security.scan(script, enforce_named_constants=False).ok

    def test_the_screw_path_emits_a_countersink(self):
        canvas = _rect(_canvas(320, 200), 20, 30, 300, 170)
        built, _, _ = plan_from_image(
            _png(canvas), KeyHolderSpec(width_mm=200.0, mount="screw", plaque_t_mm=5.0)
        )
        script = emit(built)
        assert "Cone(" in script
        ast.parse(script)

    def test_a_plan_with_no_mounts_emits_no_mount_code(self):
        canvas = _rect(_canvas(320, 200), 20, 30, 300, 170)
        built, _, _ = plan_from_image(_png(canvas), KeyHolderSpec(mount="none"))
        script = emit(built)
        assert "MOUNTS_MM = []" in script
        assert "SlotCenterToCenter" not in script


class TestRepairLadder:
    def test_a_thin_wall_reopens_the_outline(self):
        spec = KeyHolderSpec()
        adjusted, note = _adjust_for_report(spec, ["key_holder.hook_root"])
        assert adjusted.min_feature_mm > spec.min_feature_mm
        assert "minimum feature size" in note

    def test_a_loose_fragment_closes_gaps_and_deepens_the_rail(self):
        spec = KeyHolderSpec()
        adjusted, _ = _adjust_for_report(spec, ["key_holder.one_piece"])
        assert adjusted.close_gaps_mm > spec.close_gaps_mm
        assert adjusted.rail_overlap_mm > spec.rail_overlap_mm

    def test_a_failure_with_no_known_remedy_stops_the_loop(self):
        adjusted, note = _adjust_for_report(KeyHolderSpec(), ["printability.build_volume"])
        assert adjusted is None and note == ""

    def test_the_ladder_runs_out(self):
        """Three rungs, then it stops rather than guessing a fourth time."""
        spec = KeyHolderSpec()
        for _ in range(12):
            adjusted, _ = _adjust_for_report(spec, ["key_holder.hook_root"])
            if adjusted is None:
                break
            spec = adjusted
        assert adjusted is None


class TestExposedInterface:
    def test_the_cli_choices_match_the_module(self):
        assert cli.KEYHOLDER_MOUNTS == MOUNT_MODES
        assert cli.KEYHOLDER_DETAIL == DETAIL_MODES

    def test_the_parameter_schema_carries_ranges(self, wide_blob_png):
        built, _, _ = plan_from_image(wide_blob_png, KeyHolderSpec())
        schema = param_schema(built)
        assert schema["width_mm"]["minimum"] == 60.0
        assert schema["mount"]["enum"] == list(MOUNT_MODES)
        assert schema["plaque_t_mm"]["default"] == built.spec.plaque_t_mm

    def test_the_category_is_one_the_dfm_rules_know(self):
        from formforge.dfm import CATEGORIES  # noqa: PLC0415
        from formforge.keyholder.pipeline import CATEGORY  # noqa: PLC0415
        from formforge.validation.invariants import CATEGORY_INVARIANTS  # noqa: PLC0415

        assert CATEGORY in CATEGORIES
        assert CATEGORY in CATEGORY_INVARIANTS


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """One real key holder, built once by the CAD kernel and shared.

    Building a solid runs OCCT and takes seconds; a per-test fixture would make
    this file slow enough that people stop running it.
    """
    from formforge.keyholder import build_key_holder

    canvas = _canvas(320, 220)
    _rect(canvas, 20, 40, 300, 180)
    _disc(canvas, 160, 40, 70)
    return build_key_holder(
        _png(canvas),
        KeyHolderSpec(width_mm=160.0, hook_count=4),
        out_dir=tmp_path_factory.mktemp("keyholder"),
        source_name="test.png",
    )


@pytest.mark.slow
class TestEndToEnd:
    """The kernel path: one build, checked from several angles."""

    def test_it_builds_and_validates(self, built):
        assert built.ok, built.result.message
        assert built.result.validation["summary"]["failures"] == 0

    def test_it_is_one_watertight_solid(self, built):
        assert built.result.stats["solids"] == 1
        report = built.result.validation
        assert next(c for c in report["checks"] if c["id"] == "topology.watertight")["passed"]

    def test_it_is_the_size_that_was_asked_for(self, built):
        width, _, depth = built.result.stats["bbox_mm"]
        assert width == pytest.approx(160.0, abs=1.0)
        assert depth == pytest.approx(
            built.plan.plaque_t_mm + built.plan.spec.hook_out_mm, abs=0.5
        )

    def test_it_ships_a_drawing_of_what_was_traced(self, built):
        from pathlib import Path

        assert Path(built.result.previews["trace"]).exists()

    def test_the_keyholes_are_really_there(self, built):
        cylinders = built.result.stats["brep_features"]["cylinders"]
        heads = [c for c in cylinders if abs(c["diameter_mm"] - 9.0) < 0.01]
        assert len(heads) >= 2

    def test_the_source_it_ships_rebuilds_the_same_model(self, built, tmp_path):
        """The bundle's promise: `python source.py` produces this solid.

        Run in the sandbox rather than in-process, which is also the second
        assertion -- the emitted script passes the static gate and the import
        guard as a script rather than as a string this test happens to trust.
        """
        from formforge.sandbox import ExecuteRequest, GeometrySandbox  # noqa: PLC0415

        again = GeometrySandbox(keep_workdir=True).execute(
            ExecuteRequest(
                source=built.result.source_code,
                language="build123d",
                enforce_named_constants=False,
            )
        )
        assert again.ok, again.message
        assert again.stats["bbox_mm"] == pytest.approx(built.result.stats["bbox_mm"], abs=0.01)
