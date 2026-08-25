"""The validation engine, against meshes built for the purpose.

Every case here is a solid constructed to have one specific defect, so a failing
assertion names the check that broke rather than "something is wrong". Building
them with trimesh primitives keeps these tests fast and independent of the CAD
kernel; the kernel path is covered by the template harness.
"""

from __future__ import annotations

import numpy as np
import pytest
import trimesh

from formforge.validation import validate_mesh
from formforge.validation.invariants import InvariantError, build_context, evaluate
from formforge.validation.mesh import MeshMeasurements
from formforge.validation.report import Severity


def measure(mesh: trimesh.Trimesh) -> MeshMeasurements:
    mesh = mesh.copy()
    mesh.merge_vertices()
    return MeshMeasurements(mesh)


@pytest.fixture
def good_plate() -> trimesh.Trimesh:
    """A 60 x 40 x 4 mm plate: thick, flat, stable, entirely printable."""
    return trimesh.creation.box(extents=(60.0, 40.0, 4.0)).apply_translation((0, 0, 2.0))


@pytest.fixture
def thin_plate() -> trimesh.Trimesh:
    """The same plate at 0.5 mm: thinner than a 0.4 mm nozzle can extrude twice."""
    return trimesh.creation.box(extents=(60.0, 40.0, 0.5)).apply_translation((0, 0, 0.25))


class TestTopology:
    def test_a_sound_solid_passes_every_tier_one_check(self, good_plate):
        report = validate_mesh(measure(good_plate))
        assert report.passed, report.agent_feedback()
        assert report.get("topology.watertight").passed
        assert report.get("topology.solid_count").measured == 1

    def test_an_open_mesh_fails_watertight(self, good_plate):
        broken = good_plate.copy()
        broken.update_faces(np.arange(len(broken.faces)) != 0)
        report = validate_mesh(measure(broken))
        assert not report.passed
        assert report.get("topology.watertight").is_hard_failure

    def test_two_disconnected_solids_are_reported(self, good_plate):
        second = good_plate.copy().apply_translation((200.0, 0, 0))
        combined = trimesh.util.concatenate([good_plate, second])
        report = validate_mesh(measure(combined))
        check = report.get("topology.solid_count")
        assert check.is_hard_failure
        assert check.measured == 2

    def test_tier_two_is_skipped_when_the_solid_is_invalid(self, good_plate):
        """A wall-thickness number taken from a broken mesh means nothing.

        Reporting it anyway would put a second, derived failure in front of the
        real one, in a report that is about to become a repair prompt.
        """
        broken = good_plate.copy()
        broken.update_faces(np.arange(len(broken.faces)) != 0)
        report = validate_mesh(measure(broken))
        assert report.get("printability.min_wall") is None
        assert any("tier2" in s for s in report.skipped)


class TestPrintability:
    def test_thin_walls_fail(self, thin_plate):
        report = validate_mesh(measure(thin_plate))
        check = report.get("printability.min_wall")
        assert check.is_hard_failure
        assert check.measured == pytest.approx(0.5, abs=0.05)
        assert check.location_mm is not None, "a thin wall must say where it is"

    def test_wall_thickness_is_measured_accurately(self, good_plate):
        report = validate_mesh(measure(good_plate))
        assert report.measurements["min_wall_mm"] == pytest.approx(4.0, abs=0.1)

    def test_a_part_larger_than_the_plate_fails(self):
        oversized = trimesh.creation.box(extents=(400.0, 400.0, 10.0))
        report = validate_mesh(measure(oversized))
        assert report.get("printability.build_volume").is_hard_failure

    def test_a_sealed_void_is_a_hard_failure(self):
        """Unprintable in resin, wasted material in FDM, invisible from outside."""
        cube = trimesh.creation.box(extents=(40.0, 40.0, 40.0))
        void = trimesh.creation.icosphere(subdivisions=3, radius=10.0)
        void.invert()
        combined = trimesh.util.concatenate([cube, void])
        report = validate_mesh(measure(combined))
        assert report.get("printability.trapped_volume").is_hard_failure

    def test_an_enclosed_void_does_not_inflate_the_solid_count(self):
        """A cube with a cavity is one object, not two.

        Counting the void's inner shell as a second solid would fail the
        solid-count check on a model whose only real problem is the cavity --
        which trapped_volume already reports, far more usefully.
        """
        cube = trimesh.creation.box(extents=(40.0, 40.0, 40.0))
        void = trimesh.creation.icosphere(subdivisions=3, radius=10.0)
        void.invert()
        measurements = measure(trimesh.util.concatenate([cube, void]))
        assert measurements.component_count == 2
        assert measurements.solid_count == 1

    def test_a_tall_narrow_part_warns_about_wobble(self):
        tower = trimesh.creation.box(extents=(10.0, 10.0, 120.0))
        report = validate_mesh(measure(tower))
        check = report.get("printability.aspect_ratio")
        assert check.is_warning
        assert check.severity is Severity.WARN
        assert report.passed, "a wobbly part is a warning, not a rejection"

    def test_thresholds_follow_the_printer(self, thin_plate):
        """The same geometry passes or fails depending on the machine."""
        from formforge.dfm import limits_for

        fine = validate_mesh(measure(thin_plate), limits=limits_for("generic_fdm_0.4"))
        coarse = validate_mesh(measure(thin_plate), limits=limits_for("generic_fdm_0.6"))
        assert fine.get("printability.min_wall").threshold < coarse.get(
            "printability.min_wall"
        ).threshold


class TestBridgeMeasurement:
    def test_a_narrow_ring_ceiling_is_not_a_wide_bridge(self):
        """A 2 mm lip round a 120 mm rim bridges trivially.

        Measuring the region's overall width instead of its distance from
        support reports 120 mm and fails a perfectly good pot. This is the
        specific false positive the metric was rewritten to remove.
        """
        outer = trimesh.creation.annulus(r_min=58.0, r_max=60.0, height=2.0)
        measurements = measure(outer)
        assert measurements.bridges.max_span_mm < 6.0

    def test_a_wide_unsupported_ceiling_is_caught(self):
        """A mushroom cap: a 6 mm stem under a 60 mm disc."""
        stem = trimesh.creation.cylinder(radius=3.0, height=30.0)
        cap = trimesh.creation.cylinder(radius=30.0, height=4.0).apply_translation(
            (0, 0, 17.0)
        )
        combined = trimesh.util.concatenate([stem, cap])
        measurements = measure(combined)
        # The cap's underside is an annulus 27 mm wide, so the worst point is
        # 13.5 mm from support and the span is twice that.
        assert measurements.bridges.max_span_mm == pytest.approx(27.0, abs=3.0)


class TestInvariantExpressions:
    def test_evaluates_comparisons_and_arithmetic(self):
        context = {"bbox": type("B", (), {"z": 4.0})(), "wall_mm": 2.4}
        assert evaluate("bbox.z >= 2.5", context)
        assert evaluate("wall_mm * 2 < bbox.z + 1", context)
        assert not evaluate("bbox.z > 10", context)

    def test_supports_the_implies_form(self):
        assert evaluate("mount == 'keyhole' implies holes >= 2", {"mount": "flush", "holes": 0})
        assert not evaluate(
            "mount == 'keyhole' implies holes >= 2", {"mount": "keyhole", "holes": 1}
        )
        assert evaluate(
            "mount == 'keyhole' implies holes >= 2", {"mount": "keyhole", "holes": 2}
        )

    def test_refuses_to_execute_arbitrary_code(self):
        """Template invariants are data. They must never become an execution path."""
        for expression in (
            "__import__('os').system('true')",
            "().__class__.__bases__",
            "open('/etc/passwd')",
        ):
            with pytest.raises(InvariantError):
                evaluate(expression, {})

    def test_names_the_available_context_when_one_is_unknown(self):
        with pytest.raises(InvariantError) as excinfo:
            evaluate("nonexistent > 1", {"min_wall": 2.0})
        assert "min_wall" in str(excinfo.value)

    def test_min_wall_uses_the_same_statistic_as_the_dfm_check(self, good_plate):
        """A template invariant and the built-in check must not disagree.

        If invariants read the raw minimum while the check reads a percentile,
        every template author would have to declare a lower bound than the rule
        they are actually held to.
        """
        measurements = measure(good_plate)
        context = build_context(measurements, {})
        assert context["min_wall"] == pytest.approx(
            measurements.thickness.p01_with_tolerance_mm
        )
        assert context["min_wall_abs"] == pytest.approx(measurements.thickness.min_mm)

    def test_a_wall_at_exactly_the_minimum_passes(self):
        """Tessellation and sampling put a 2.0 mm wall a few microns low.

        Without a measurement tolerance, every part built at exactly the minimum
        its own schema allows fails -- and "the minimum" is precisely what a
        careful template author picks.
        """
        plate = trimesh.creation.box(extents=(60.0, 40.0, 2.0))
        measurements = measure(plate)
        context = build_context(measurements, {})
        assert context["min_wall"] >= 2.0


class TestReport:
    def test_agent_feedback_carries_the_numbers(self, thin_plate):
        report = validate_mesh(measure(thin_plate))
        feedback = report.agent_feedback()
        assert "VALIDATION FAILED" in feedback
        assert "min_wall" in feedback
        assert "0.5" in feedback, "the model needs the measured value, not just a verdict"

    def test_serialises_to_json(self, good_plate):
        import json

        report = validate_mesh(measure(good_plate))
        payload = json.loads(report.to_json())
        assert payload["passed"] is True
        assert payload["summary"]["checks"] > 10

    def test_warnings_do_not_block_delivery(self):
        tower = trimesh.creation.box(extents=(10.0, 10.0, 120.0))
        report = validate_mesh(measure(tower))
        assert report.warnings
        assert report.passed
