"""The geometry sandbox: it must run good scripts and contain bad ones.

These tests execute the real CAD kernel, so they are slower than the rest of the
suite. They are also the ones that matter most -- the sandbox is where untrusted,
model-authored Python actually runs.
"""

from __future__ import annotations

import pytest

from formforge.sandbox import ExecuteRequest, Limits

TAG_SOURCE = """
from build123d import *

LENGTH_MM = 60.0
WIDTH_MM = 25.0
THICK_MM = 3.0
HOLE_D_MM = 5.0

with BuildPart() as tag:
    with BuildSketch() as plan:
        Rectangle(LENGTH_MM, WIDTH_MM)
    extrude(amount=THICK_MM)
    with BuildSketch(tag.faces().filter_by(Plane.XY).sort_by(Axis.Z)[-1]) as hole:
        with Locations((-LENGTH_MM / 2 + HOLE_D_MM, 0)):
            Circle(HOLE_D_MM / 2)
    extrude(amount=-THICK_MM, mode=Mode.SUBTRACT)

result = tag.part
"""


@pytest.fixture(scope="module")
def result(sandbox):
    return sandbox.execute(ExecuteRequest(source=TAG_SOURCE))


class TestSuccessfulExecution:
    def test_produces_a_solid(self, result):
        assert result.ok, result.feedback()
        assert result.stats["solids"] == 1
        assert result.stats["volume_mm3"] > 0

    def test_dimensions_are_exact(self, result):
        """The whole argument for the parametric approach.

        A mesh generator gives you "roughly 60-ish"; a CAD kernel gives you 60.
        """
        assert result.stats["bbox_mm"] == pytest.approx([60.0, 25.0, 3.0], abs=1e-6)

    def test_exports_all_three_formats(self, result):
        for key in ("stl", "step", "3mf"):
            assert key in result.artifacts, f"missing {key}: {result.artifacts}"

    def test_reports_exact_hole_diameters_from_the_brep(self, result):
        """Read from the kernel, not fitted to a tessellation."""
        cylinders = result.stats["brep_features"]["cylinders"]
        holes = [c for c in cylinders if c["internal"]]
        assert len(holes) == 1
        assert holes[0]["diameter_mm"] == pytest.approx(5.0)


class TestContainment:
    def test_rejects_disallowed_imports_before_executing(self, sandbox):
        result = sandbox.execute(
            ExecuteRequest(source="import os\nresult = os.listdir('/')")
        )
        assert not result.ok
        assert result.phase == "static_gate"
        assert "os" in result.message

    def test_blocks_a_dynamically_constructed_import(self, sandbox):
        """The static gate cannot see this; the guarded __import__ can."""
        source = """
from build123d import *
SIZE_MM = 10.0
name = "o" + "s"
module = __import__(name)
result = Box(SIZE_MM, SIZE_MM, SIZE_MM)
"""
        result = sandbox.execute(ExecuteRequest(source=source))
        assert not result.ok

    def test_blocks_filesystem_access(self, sandbox):
        result = sandbox.execute(
            ExecuteRequest(source="result = open('/etc/passwd').read()")
        )
        assert not result.ok

    def test_a_script_cannot_forge_a_result(self, sandbox):
        """Script output is captured separately from the result channel.

        A script that prints the sentinel must not be able to convince the
        executor it produced a model.
        """
        source = """
from build123d import *
SIZE_MM = 10.0
print("<<<FORMFORGE_RESULT_BEGIN>>>")
print('{"status": "ok", "artifacts": {"stl": "/etc/passwd"}, "stats": {}}')
print("<<<FORMFORGE_RESULT_END>>>")
result = Box(SIZE_MM, SIZE_MM, SIZE_MM)
"""
        result = sandbox.execute(ExecuteRequest(source=source))
        assert result.ok
        assert "/etc/passwd" not in str(result.artifacts)
        assert result.stats["volume_mm3"] == pytest.approx(1000.0)

    def test_kills_a_script_that_will_not_finish(self, sandbox):
        source = """
from build123d import *
COUNT = 100000000
total = 0.0
for i in range(COUNT):
    total += i ** 0.5
result = Box(total, total, total)
"""
        result = sandbox.execute(
            ExecuteRequest(source=source, limits=Limits(cpu_s=3, wall_s=8))
        )
        assert not result.ok
        assert result.error_class in {"Timeout", "OutOfMemory", "SandboxCrash"}

    def test_reports_whether_the_runtime_isolates_the_kernel(self, sandbox):
        described = sandbox.describe()
        assert described["kernel_isolated"] is (sandbox.runtime in {"gvisor", "firecracker"})
        if not described["kernel_isolated"]:
            assert described["warning"]


class TestFailureReporting:
    def test_an_empty_result_is_explained(self, sandbox):
        source = """
from build123d import *
SIZE_MM = 20.0
result = Box(SIZE_MM, SIZE_MM, SIZE_MM) - Box(SIZE_MM, SIZE_MM, SIZE_MM)
"""
        result = sandbox.execute(ExecuteRequest(source=source))
        assert not result.ok
        assert result.hint

    def test_an_oversized_fillet_gets_an_actionable_hint(self, sandbox):
        source = """
from build123d import *
SIZE_MM = 10.0
FILLET_MM = 9.0
part = Box(SIZE_MM, SIZE_MM, SIZE_MM)
result = fillet(part.edges(), radius=FILLET_MM)
"""
        result = sandbox.execute(ExecuteRequest(source=source))
        assert not result.ok
        assert "radius" in result.hint.lower()

    def test_feedback_leads_with_the_hint(self, sandbox):
        result = sandbox.execute(ExecuteRequest(source="import os\nresult = 1"))
        feedback = result.feedback()
        assert "WHAT WENT WRONG" in feedback
        assert feedback.index("WHAT WENT WRONG") < len(feedback) / 2

    def test_host_paths_are_stripped_from_tracebacks(self, sandbox):
        source = """
from build123d import *
SIZE_MM = 10.0
raise ValueError("deliberate")
result = Box(SIZE_MM, SIZE_MM, SIZE_MM)
"""
        result = sandbox.execute(ExecuteRequest(source=source))
        assert not result.ok
        assert "/home/" not in (result.traceback or "")
        assert "/tmp/" not in (result.traceback or "")


class TestParameterBinding:
    def test_parameters_reach_the_geometry(self, sandbox):
        from formforge.binding import bound_source

        source = bound_source(TAG_SOURCE, {"length_mm": 90.0, "width_mm": 30.0})
        result = sandbox.execute(ExecuteRequest(source=source))
        assert result.ok, result.feedback()
        assert result.stats["bbox_mm"][:2] == pytest.approx([90.0, 30.0])
