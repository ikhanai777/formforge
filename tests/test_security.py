"""The static gate must reject the dangerous and accept the ordinary.

A gate that blocks a legitimate script is as much a bug as one that lets a
malicious one through: it fails a user's generation with a confusing message
for something they did not do wrong.
"""

from __future__ import annotations

import pytest

from formforge import security

GOOD_SCRIPT = """
from build123d import *

WIDTH_MM = 60.0
HEIGHT_MM = 4.0
RADIUS_MM = 3.0

with BuildPart() as part:
    with BuildSketch() as plan:
        RectangleRounded(WIDTH_MM, WIDTH_MM, RADIUS_MM)
    extrude(amount=HEIGHT_MM)
result = part.part
"""


def test_accepts_a_well_formed_script():
    result = security.scan(GOOD_SCRIPT)
    assert result.ok, result.report()
    assert result.constants["WIDTH_MM"] == 60.0
    assert "build123d" in result.imports


@pytest.mark.parametrize(
    "snippet,rule",
    [
        ("import os\nresult = 1", "import"),
        ("import subprocess\nresult = 1", "import"),
        ("from socket import socket\nresult = 1", "import"),
        ("import requests\nresult = 1", "import"),
        ("from . import sibling\nresult = 1", "import"),
        ("result = eval('1+1')", "banned-name"),
        ("result = open('/etc/passwd').read()", "banned-name"),
        ("result = __import__('os')", "banned-name"),
        ("result = (1).__class__.__bases__", "dunder-attribute"),
        ("while True:\n    pass\nresult = 1", "unbounded-while"),
        ("def f(:\n", "syntax"),
    ],
)
def test_rejects_dangerous_constructs(snippet, rule):
    result = security.scan(snippet)
    assert not result.ok
    assert rule in {v.rule for v in result.errors}


def test_rejects_magic_numbers_in_geometry_calls():
    result = security.scan("from build123d import *\nresult = Box(60, 40, 3)")
    assert not result.ok
    assert "magic-number" in {v.rule for v in result.errors}


def test_allows_expressions_built_from_named_constants():
    source = """
from build123d import *
WIDTH_MM = 60.0
WALL_MM = 2.4
result = Box(WIDTH_MM, WIDTH_MM - 2 * WALL_MM, WALL_MM)
"""
    assert security.scan(source).ok


def test_structural_literals_are_not_magic_numbers():
    """Counts, flags and axis indices are not dimensions.

    Forcing a named constant for `align=(Align.CENTER,)` or a segment count of 6
    produces noise, not editability.
    """
    source = """
from build123d import *
RADIUS_MM = 30.0
result = RegularPolygon(RADIUS_MM, 6, major_radius=False)
"""
    assert security.scan(source).ok


def test_named_constant_rule_can_be_relaxed_for_reviewed_templates():
    source = "from build123d import *\nresult = Box(60, 40, 3)"
    assert security.scan(source, enforce_named_constants=False).ok


def test_bounded_while_loop_is_accepted():
    source = """
from build123d import *
COUNT = 5
i = 0
while i < COUNT:
    i += 1
result = i
"""
    assert security.scan(source).ok


def test_enforce_raises_with_a_readable_report():
    with pytest.raises(security.SecurityError) as excinfo:
        security.enforce("import os\nresult = 1")
    assert "os" in str(excinfo.value)
