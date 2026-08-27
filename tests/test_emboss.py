"""Tracing an image into a relief.

The pipeline's whole claim is that it infers nothing: a silhouette is exact
where a single-image mesh generator is guessing. So the tests are mostly about
shapes whose right answer is known in advance -- a disc with a hole has one
hole, five letters are five shapes -- plus the two places the geometry has
historically gone wrong.
"""

from __future__ import annotations

import ast
import contextlib

import numpy as np
import pytest

from formforge.emboss import (
    EmbossOptions,
    emboss_source,
    load_mask,
    trace_polygons,
)

Image = pytest.importorskip("PIL.Image")
ImageDraw = pytest.importorskip("PIL.ImageDraw")


def _disc_with_hole(tmp_path):
    img = Image.new("RGB", (400, 400), "white")
    draw = ImageDraw.Draw(img)
    draw.ellipse([60, 60, 340, 340], fill="black")
    draw.ellipse([160, 160, 240, 240], fill="white")
    path = tmp_path / "donut.png"
    img.save(path)
    return path


def _two_blobs(tmp_path):
    img = Image.new("RGB", (400, 300), "white")
    draw = ImageDraw.Draw(img)
    draw.ellipse([30, 60, 170, 240], fill="black")
    draw.rectangle([220, 80, 360, 220], fill="black")
    draw.ellipse([198, 20, 202, 24], fill="black")  # a speck
    path = tmp_path / "two.png"
    img.save(path)
    return path


class TestTracing:
    def test_a_disc_with_a_hole_traces_as_one_shape_with_one_hole(self, tmp_path):
        opts = EmbossOptions(width_mm=100.0, margin_mm=0.0)
        trace = trace_polygons(load_mask(_disc_with_hole(tmp_path), opts), opts)
        assert len(trace.polygons) == 1
        assert trace.holes == 1

    def test_the_traced_area_matches_the_drawn_area(self, tmp_path):
        shapely = pytest.importorskip("shapely.geometry")
        opts = EmbossOptions(width_mm=100.0, margin_mm=0.0)
        trace = trace_polygons(load_mask(_disc_with_hole(tmp_path), opts), opts)
        shell, holes = trace.polygons[0]
        area = shapely.Polygon(shell, holes).area
        # 280 px across becomes 100 mm, so the ring is r=50 with a hole of
        # r=50*80/280. Tolerance is for smoothing and simplification, not slop.
        expected = np.pi * (50.0**2 - (50.0 * 80 / 280) ** 2)
        assert area == pytest.approx(expected, rel=0.03)

    def test_specks_are_dropped_and_real_shapes_are_kept(self, tmp_path):
        opts = EmbossOptions(width_mm=100.0, margin_mm=0.0)
        trace = trace_polygons(load_mask(_two_blobs(tmp_path), opts), opts)
        assert len(trace.polygons) == 2
        assert any("speck" in note for note in trace.notes)

    def test_smoothing_takes_the_contour_off_the_pixel_lattice(self, tmp_path):
        """The tracer walks pixel edges, so every raw vertex is an integer.

        A diagonal made of integer points is a staircase no simplification can
        remove, because simplification only ever picks a subset of the points
        it was given.
        """
        opts = EmbossOptions(width_mm=100.0, margin_mm=0.0, simplify_px=0.0)
        trace = trace_polygons(load_mask(_disc_with_hole(tmp_path), opts), opts)
        shell, _ = trace.polygons[0]
        xs = np.array([x for x, _ in shell])
        assert not np.allclose(xs, np.round(xs))


def _island_in_a_hole(tmp_path):
    """Disc, hole, and a smaller disc sitting inside that hole."""
    img = Image.new("RGB", (400, 400), "white")
    draw = ImageDraw.Draw(img)
    draw.ellipse([40, 40, 360, 360], fill="black")
    draw.ellipse([120, 120, 280, 280], fill="white")
    draw.ellipse([170, 170, 230, 230], fill="black")
    path = tmp_path / "island.png"
    img.save(path)
    return path


class TestNesting:
    def test_an_island_inside_a_hole_comes_back_solid(self, tmp_path):
        """Depth two is solid, not another hole.

        Treating it as a second hole of the same shape puts two overlapping
        holes in one face, and the face that comes out of that is open -- which
        is how a photograph produced a mesh the validator rejected as not
        watertight.
        """
        opts = EmbossOptions(width_mm=100.0, margin_mm=0.0)
        trace = trace_polygons(load_mask(_island_in_a_hole(tmp_path), opts), opts)
        assert len(trace.polygons) == 2, "the island should be its own shape"
        assert trace.holes == 1, "only the ring's hole is a hole"

    def test_containment_needs_the_area_guard(self, tmp_path):
        """A disc's representative point lands inside its own hole.

        Without requiring a container to be strictly larger, the hole reads as
        containing its parent, both come out at odd depth, and nothing is left
        that counts as a shape at all.
        """
        opts = EmbossOptions(width_mm=100.0, margin_mm=0.0)
        trace = trace_polygons(load_mask(_disc_with_hole(tmp_path), opts), opts)
        assert trace.polygons, "the disc vanished entirely"

    def test_fill_discards_interior_holes(self, tmp_path):
        opts = EmbossOptions(width_mm=100.0, margin_mm=0.0, fill_holes=True)
        trace = trace_polygons(load_mask(_island_in_a_hole(tmp_path), opts), opts)
        assert trace.holes == 0
        assert len(trace.polygons) == 1


class TestEmittedSource:
    def test_the_source_parses_and_keeps_printability_parametric(self, tmp_path):
        opts = EmbossOptions(width_mm=120.0)
        trace = trace_polygons(load_mask(_disc_with_hole(tmp_path), opts), opts)
        source = emboss_source(trace, opts, "donut.png")
        tree = ast.parse(source)
        names = {
            node.targets[0].id
            for node in tree.body
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
        }
        # The contour is data; everything that decides whether it prints is not.
        assert {"PANEL_W_MM", "PANEL_T_MM", "RELIEF_MM", "HANG_D_MM"} <= names

    def test_the_hanging_hole_clears_the_art(self, tmp_path):
        """At the default inset the hole otherwise lands inside the artwork."""
        opts = EmbossOptions(width_mm=120.0)
        trace = trace_polygons(load_mask(_disc_with_hole(tmp_path), opts), opts)
        source = emboss_source(trace, opts, "donut.png")
        consts = {}
        for node in ast.parse(source).body:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                with contextlib.suppress(ValueError):
                    consts[node.targets[0].id] = ast.literal_eval(node.value)
        top_of_art = max(y for shell, _ in trace.polygons for _, y in shell)
        top_of_art += consts["ART_Y_MM"]
        hole_bottom = (
            consts["PANEL_H_MM"] / 2 - consts["HANG_INSET_MM"] - consts["HANG_D_MM"] / 2
        )
        assert hole_bottom > top_of_art

    def test_a_standalone_cut_has_no_panel(self, tmp_path):
        opts = EmbossOptions(width_mm=120.0, standalone=True)
        trace = trace_polygons(load_mask(_disc_with_hole(tmp_path), opts), opts)
        source = emboss_source(trace, opts, "donut.png")
        assert "RectangleRounded(PANEL_W_MM" not in source
        ast.parse(source)


@pytest.mark.slow
class TestBuildsAndValidates:
    def test_the_traced_relief_builds_a_watertight_solid(self, tmp_path):
        from formforge.sandbox import ExecuteRequest, GeometrySandbox

        opts = EmbossOptions(width_mm=120.0)
        trace = trace_polygons(load_mask(_disc_with_hole(tmp_path), opts), opts)
        source = emboss_source(trace, opts, "donut.png")
        sandbox = GeometrySandbox()
        result = sandbox.execute(
            ExecuteRequest(source=source, enforce_named_constants=False)
        )
        assert result.ok, result.feedback()
        assert result.stats["solids"] == 1
        assert result.stats["shells"] == 1

        # The claim is a clean solid, so let the validator say so rather than
        # trusting the kernel's own word for it.
        from formforge.validation import validate

        report = validate(result.artifacts["stl"])
        measured = report.measurements
        assert measured["watertight"]
        assert measured["self_intersections"] == 0
        assert measured["degenerate_faces"] == 0
