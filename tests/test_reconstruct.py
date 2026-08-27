"""The reconstruction adapter, and the clean-up that makes its output printable.

The model itself is not exercised here -- it lives on the user's machine and
needs a GPU. What is exercised is everything FormForge actually contributes:
refusing a mesh with no units, telling a body from a floater, letting the lossy
repair rungs run on a path that has no STEP file to contradict, and seating the
result on the plate.
"""

from __future__ import annotations

import numpy as np
import pytest
import trimesh

from formforge.reconstruct import (
    ReconstructError,
    ReconstructOptions,
    clean,
    is_local,
    reconstruct,
)


def _torn_box(extents=(1.0, 1.0, 1.6), drop=2):
    mesh = trimesh.creation.box(extents=extents).subdivide()
    mesh.faces = mesh.faces[:-drop]
    return trimesh.Trimesh(vertices=mesh.vertices, faces=mesh.faces, process=False)


class TestUnits:
    def test_a_mesh_with_no_size_is_refused(self, tmp_path):
        """A generative mesh is unitless, and guessing is the one thing this
        system is not allowed to do about dimensions."""
        image = tmp_path / "view.png"
        image.write_bytes(b"not really an image")
        with pytest.raises(ReconstructError) as excinfo:
            reconstruct([image], ReconstructOptions(size_mm=0))
        assert "no units" in str(excinfo.value)

    def test_scaling_hits_the_requested_axis(self):
        mesh = trimesh.creation.box(extents=(1.0, 2.0, 4.0))
        out = clean(mesh, ReconstructOptions(size_mm=200.0, size_axis="z"))
        assert float(np.ptp(out.mesh.vertices[:, 2])) == pytest.approx(200.0)
        # The other axes follow, because scaling is uniform.
        assert float(np.ptp(out.mesh.vertices[:, 0])) == pytest.approx(50.0)

    def test_scaling_can_key_on_width_instead(self):
        mesh = trimesh.creation.box(extents=(1.0, 2.0, 4.0))
        out = clean(mesh, ReconstructOptions(size_mm=100.0, size_axis="x"))
        assert float(np.ptp(out.mesh.vertices[:, 0])) == pytest.approx(100.0)


class TestFloaters:
    def test_a_small_floater_is_dropped(self):
        body = trimesh.creation.box(extents=(1.0, 1.0, 1.6))
        shard = trimesh.creation.box(extents=(0.05, 0.05, 0.05))
        shard.apply_translation([3.0, 3.0, 3.0])
        combined = trimesh.util.concatenate([body, shard])
        out = clean(combined, ReconstructOptions(size_mm=100.0))
        assert out.dropped_components == 1
        assert len(out.mesh.split(only_watertight=False)) == 1

    def test_size_is_bulk_and_not_triangle_count(self):
        """A coarse body beside a finely tessellated speck.

        Ranking components by face count measures how finely each was
        tessellated rather than how big it is, and gets this exactly backwards:
        the speck wins and the body is discarded as the floater.
        """
        body = trimesh.creation.box(extents=(1.0, 1.0, 1.6))          # 12 faces
        speck = trimesh.creation.icosphere(subdivisions=3, radius=0.04)  # ~1280
        speck.apply_translation([4.0, 4.0, 4.0])
        assert len(speck.faces) > len(body.faces)
        combined = trimesh.util.concatenate([body, speck])
        out = clean(combined, ReconstructOptions(size_mm=100.0))
        assert out.dropped_components == 1
        kept = float(np.ptp(out.mesh.vertices[:, 2]))
        assert kept == pytest.approx(100.0), "the body should be what survived"


class TestRepair:
    def test_lossy_rungs_run_on_this_path(self):
        """`repair.py` refuses lossy rungs on the parametric path because they
        make the STL disagree with the STEP file. A reconstruction has no STEP
        file, so there is nothing for a patched mesh to contradict."""
        torn = _torn_box()
        assert not torn.is_watertight
        out = clean(torn, ReconstructOptions(size_mm=100.0, allow_lossy_repair=True))
        assert out.repair["changed"]
        assert out.mesh.is_watertight

    def test_refusing_lossy_leaves_it_torn(self):
        torn = _torn_box()
        out = clean(torn, ReconstructOptions(size_mm=100.0, allow_lossy_repair=False))
        assert not out.mesh.is_watertight


class TestPlacement:
    def test_the_mesh_is_seated_on_the_plate(self):
        mesh = trimesh.creation.box(extents=(1.0, 1.0, 1.0))
        mesh.apply_translation([5.0, -3.0, 12.0])
        out = clean(mesh, ReconstructOptions(size_mm=50.0))
        bounds = out.mesh.bounds
        assert bounds[0][2] == pytest.approx(0.0), "z=0 is the bed"
        assert (bounds[0][0] + bounds[1][0]) / 2 == pytest.approx(0.0)
        assert (bounds[0][1] + bounds[1][1]) / 2 == pytest.approx(0.0)


class TestBackend:
    def test_an_unreachable_backend_says_what_to_do(self, tmp_path):
        image = tmp_path / "view.png"
        image.write_bytes(b"x")
        opts = ReconstructOptions(url="http://127.0.0.1:9", size_mm=100.0, timeout_s=2)
        with pytest.raises(ReconstructError) as excinfo:
            reconstruct([image], opts)
        message = str(excinfo.value)
        assert "could not reach" in message
        assert "--url" in message

    def test_loopback_is_recognised(self):
        assert is_local("http://127.0.0.1:8300")
        assert is_local("http://localhost:8300")
        assert not is_local("http://gpu.example.com:8300")
