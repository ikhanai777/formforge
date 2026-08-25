"""Parameter binding rewrites constants without destroying the script.

The bundle's `source.py` is the artifact the whole parametric approach exists to
produce, so "the values are right" is only half the requirement -- it also has
to still be a file a person would want to read.
"""

from __future__ import annotations

import ast

import pytest

from formforge import binding

SOURCE = '''\
from build123d import *

# The overall footprint.
WIDTH_MM = 60.0
HEIGHT_MM = 4.0
LABEL = "HELLO"
ENGRAVE = False

# Derived, not a parameter.
INNER_MM = WIDTH_MM - 2.0


def helper():
    WIDTH_MM = 999.0  # a local, not the module constant
    return WIDTH_MM


result = Box(WIDTH_MM, WIDTH_MM, HEIGHT_MM)
'''


def test_binds_declared_constants():
    result = binding.bind(SOURCE, {"width_mm": 120.0, "label": "RIVER", "engrave": True})
    assert result.ok
    constants = binding.declared_constants(result.source)
    assert constants["WIDTH_MM"] == 120.0
    assert constants["LABEL"] == "RIVER"
    assert constants["ENGRAVE"] is True
    assert constants["HEIGHT_MM"] == 4.0


def test_preserves_comments_and_formatting():
    """The comments are where the reasoning lives; an AST round-trip loses them."""
    bound = binding.bound_source(SOURCE, {"width_mm": 120.0})
    assert "# The overall footprint." in bound
    assert "# Derived, not a parameter." in bound
    assert "# a local, not the module constant" in bound


def test_does_not_touch_a_same_named_local():
    bound = binding.bound_source(SOURCE, {"width_mm": 120.0})
    tree = ast.parse(bound)
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef))
    assigned = next(n for n in function.body if isinstance(n, ast.Assign))
    assert assigned.value.value == 999.0


def test_leaves_derived_expressions_alone():
    bound = binding.bound_source(SOURCE, {"width_mm": 120.0})
    assert "INNER_MM = WIDTH_MM - 2.0" in bound


def test_reports_parameters_with_no_matching_constant():
    result = binding.bind(SOURCE, {"width_mm": 120.0, "nonexistent_mm": 5.0})
    assert not result.ok
    assert "nonexistent_mm" in result.unbound


def test_strict_mode_raises_on_drift():
    """A schema and a script that disagree must not fail silently.

    Silent drift means a model built with the wrong dimensions and no error
    anywhere, which is the worst failure this system could have.
    """
    with pytest.raises(binding.BindingError):
        binding.bind(SOURCE, {"nonexistent_mm": 5.0}, strict=True)


def test_bound_source_still_parses():
    bound = binding.bound_source(SOURCE, {"width_mm": 120.0, "label": "A'B\"C"})
    ast.parse(bound)
    assert binding.declared_constants(bound)["LABEL"] == "A'B\"C"


def test_extract_params_filters_by_schema():
    schema = {"type": "object", "properties": {"width_mm": {}, "height_mm": {}}}
    params = binding.extract_params(SOURCE, schema)
    assert set(params) == {"width_mm", "height_mm"}
    assert params["width_mm"] == 60.0


def test_no_values_is_a_no_op():
    result = binding.bind(SOURCE, {})
    assert result.source == SOURCE
