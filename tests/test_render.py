"""Rendering: the images must be valid, readable and reproducible.

Reproducibility is not fussiness. The renders feed the visual critique, so a
renderer that varies between runs makes that step non-deterministic and its
regressions impossible to bisect.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np
import pytest
import trimesh

from formforge.render import CRITIQUE_VIEWS, STANDARD_VIEWS, render_views
from formforge.render.raster import BACKGROUND_RGB, SECTION_RGB, write_png


def read_png(path: Path) -> np.ndarray:
    """Decode our own PNGs, so the tests do not need an imaging library."""
    data = Path(path).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"

    offset = 8
    width = height = 0
    idat = bytearray()
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        tag = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        if tag == b"IHDR":
            width, height, depth, colour = struct.unpack(">IIBB", payload[:10])
            assert depth == 8 and colour == 2, "expected 8-bit RGB"
        elif tag == b"IDAT":
            idat += payload
        elif tag == b"IEND":
            break
        offset += 12 + length

    raw = zlib.decompress(bytes(idat))
    stride = width * 3
    pixels = np.zeros((height, width, 3), dtype=np.uint8)
    for row in range(height):
        start = row * (stride + 1)
        assert raw[start] == 0, "only filter type 0 is written"
        pixels[row] = np.frombuffer(
            raw[start + 1 : start + 1 + stride], dtype=np.uint8
        ).reshape(width, 3)
    return pixels


@pytest.fixture(scope="module")
def hollow_pot(tmp_path_factory) -> Path:
    """A cup: something with an inside worth looking at."""
    outer = trimesh.creation.cylinder(radius=30.0, height=60.0)
    inner = trimesh.creation.cylinder(radius=27.0, height=56.0).apply_translation(
        (0, 0, 4.0)
    )
    inner.invert()
    mesh = trimesh.util.concatenate([outer, inner])
    mesh.apply_translation((0, 0, 30.0))
    path = tmp_path_factory.mktemp("render") / "pot.stl"
    mesh.export(path)
    return path


class TestPngEncoder:
    def test_round_trips_pixels(self, tmp_path):
        source = np.random.default_rng(0).integers(0, 256, (16, 24, 3), dtype=np.uint8)
        path = write_png(tmp_path / "x.png", source)
        assert np.array_equal(read_png(path), source)

    def test_rejects_a_non_rgb_array(self, tmp_path):
        with pytest.raises(ValueError):
            write_png(tmp_path / "x.png", np.zeros((4, 4), dtype=np.uint8))


class TestRendering:
    def test_renders_every_standard_view(self, hollow_pot, tmp_path):
        result = render_views(hollow_pot, tmp_path, views=STANDARD_VIEWS)
        assert set(result.views) == set(STANDARD_VIEWS)
        for path in result.views.values():
            assert Path(path).exists()

    def test_images_are_the_requested_size(self, hollow_pot, tmp_path):
        result = render_views(hollow_pot, tmp_path, views=("iso",), size=256)
        assert read_png(Path(result.views["iso"])).shape == (256, 256, 3)

    def test_the_model_actually_appears(self, hollow_pot, tmp_path):
        """A blank frame would pass every other check here."""
        result = render_views(hollow_pot, tmp_path, views=("front",))
        pixels = read_png(Path(result.views["front"]))
        background = (np.asarray(BACKGROUND_RGB) * 255).astype(np.uint8)
        non_background = np.any(np.abs(pixels.astype(int) - background) > 8, axis=2)
        covered = non_background.mean()
        assert 0.05 < covered < 0.95, f"{covered:.1%} of the frame is not background"

    def test_the_section_view_shows_a_cut_face(self, hollow_pot, tmp_path):
        """Without a visible cut face, a section view is just another view.

        The whole reason the section exists is to show what the outside hides,
        and the model reading the render has to be able to tell which surface is
        the cut.
        """
        result = render_views(hollow_pot, tmp_path, views=("section",))
        pixels = read_png(Path(result.views["section"]))
        section = (np.asarray(SECTION_RGB) * 255).astype(np.uint8)
        matches = np.all(np.abs(pixels.astype(int) - section) <= 2, axis=2)
        assert matches.sum() > 200, "the cut face is not visible in the section view"

    def test_the_iso_view_draws_the_build_plate_grid(self, hollow_pot, tmp_path):
        """The grid is the only absolute scale cue a vision model gets."""
        with_grid = read_png(
            Path(render_views(hollow_pot, tmp_path / "a", views=("iso",)).views["iso"])
        )
        without = read_png(
            Path(
                render_views(
                    hollow_pot, tmp_path / "b", views=("iso",), show_build_plate_grid=False
                ).views["iso"]
            )
        )
        assert not np.array_equal(with_grid, without)

    def test_output_is_reproducible(self, hollow_pot, tmp_path):
        first = render_views(hollow_pot, tmp_path / "1", views=CRITIQUE_VIEWS)
        second = render_views(hollow_pot, tmp_path / "2", views=CRITIQUE_VIEWS)
        for name in CRITIQUE_VIEWS:
            assert Path(first.views[name]).read_bytes() == Path(second.views[name]).read_bytes()

    def test_builds_a_contact_sheet(self, hollow_pot, tmp_path):
        result = render_views(hollow_pot, tmp_path, views=CRITIQUE_VIEWS)
        assert result.contact_sheet
        sheet = read_png(Path(result.contact_sheet))
        assert sheet.shape[0] > 512 and sheet.shape[1] > 512

    def test_an_unknown_view_name_says_what_is_available(self, hollow_pot, tmp_path):
        with pytest.raises(ValueError) as excinfo:
            render_views(hollow_pot, tmp_path, views=("sideways",))
        assert "iso" in str(excinfo.value)
