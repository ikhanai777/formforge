"""STL -> STEP conversion.

Each case builds a mesh with trimesh (fast, kernel-independent to construct)
and then runs it through the real conversion, which does invoke build123d/OCCT
-- there is no meaningful way to fake "does OCCT accept this shell", so unlike
the pure-mesh validation tests this one pays the kernel cost.
"""

from __future__ import annotations

import numpy as np
import pytest
import trimesh

from formforge.mesh_to_step import convert_stl_to_step


def _write(mesh: trimesh.Trimesh, scratch, name: str = "model.stl"):
    path = scratch / name
    mesh.export(path)
    return path


class TestWatertightMesh:
    def test_a_closed_box_becomes_a_solid_with_the_right_volume(self, scratch):
        box = trimesh.creation.box(extents=(10.0, 20.0, 30.0))
        stl_path = _write(box, scratch)
        result = convert_stl_to_step(stl_path, scratch / "model.step")

        assert result.is_manifold
        assert result.volume_mm3 == pytest.approx(10.0 * 20.0 * 30.0, rel=1e-6)
        assert (scratch / "model.step").exists()
        assert not result.warnings

    def test_coplanar_triangles_merge_into_one_face_per_side(self, scratch):
        box = trimesh.creation.box(extents=(10.0, 20.0, 30.0))
        stl_path = _write(box, scratch)

        merged = convert_stl_to_step(stl_path, scratch / "merged.step", merge_coplanar=True)
        unmerged = convert_stl_to_step(
            stl_path, scratch / "unmerged.step", merge_coplanar=False
        )

        # A box has 6 sides, each 2 triangles; merging collapses each side's
        # pair of coplanar triangles into a single face without changing volume.
        assert merged.face_count == 6
        assert unmerged.face_count == 12
        assert merged.volume_mm3 == pytest.approx(unmerged.volume_mm3, rel=1e-9)

    def test_input_unit_scales_into_the_mm_the_step_file_declares(self, scratch):
        box = trimesh.creation.box(extents=(1.0, 1.0, 1.0))
        stl_path = _write(box, scratch)
        result = convert_stl_to_step(stl_path, scratch / "model.step", input_unit="in")
        assert result.volume_mm3 == pytest.approx(25.4**3, rel=1e-6)


class TestDegenerateInput:
    def test_zero_area_triangles_are_dropped_and_reported(self, scratch):
        vertices = np.array(
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 0.0]]
        )
        # One real triangle plus one degenerate (repeated vertex index).
        faces = np.array([[0, 1, 2], [0, 0, 1]])
        mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
        stl_path = _write(mesh, scratch)

        result = convert_stl_to_step(stl_path, scratch / "model.step")
        assert result.dropped_degenerate == 1
        assert any("degenerate" in w for w in result.warnings)

    def test_an_open_mesh_writes_a_shell_not_a_solid(self, scratch):
        sphere = trimesh.creation.icosphere(subdivisions=1, radius=5.0)
        sphere.update_faces(np.arange(len(sphere.faces)) != 0)  # drop one face
        stl_path = _write(sphere, scratch)

        result = convert_stl_to_step(stl_path, scratch / "model.step")
        assert not result.is_manifold
        assert any("not watertight" in w for w in result.warnings)
        assert (scratch / "model.step").exists()

    def test_all_degenerate_triangles_refuses_rather_than_writing_nothing(self, scratch):
        vertices = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
        faces = np.array([[0, 0, 1]])
        mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
        stl_path = _write(mesh, scratch)

        with pytest.raises(ValueError, match="non-degenerate"):
            convert_stl_to_step(stl_path, scratch / "model.step")


class TestInputValidation:
    def test_missing_file_raises_a_clear_error(self, scratch):
        with pytest.raises(ValueError, match="could not read"):
            convert_stl_to_step(scratch / "nope.stl", scratch / "model.step")

    def test_unknown_unit_raises_a_clear_error(self, scratch):
        box = trimesh.creation.box(extents=(1.0, 1.0, 1.0))
        stl_path = _write(box, scratch)
        with pytest.raises(ValueError, match="input_unit"):
            convert_stl_to_step(stl_path, scratch / "model.step", input_unit="parsec")

    def test_over_the_triangle_cap_refuses_rather_than_hanging(self, scratch):
        sphere = trimesh.creation.icosphere(subdivisions=2, radius=5.0)
        stl_path = _write(sphere, scratch)
        with pytest.raises(ValueError, match="over the"):
            convert_stl_to_step(stl_path, scratch / "model.step", max_triangles=10)
