"""The template registry: loading, validation, matching and routing.

The load-time checks matter more than they look. A template whose schema and
script have drifted apart produces a model with the wrong dimensions and no
error anywhere -- the parameters bind to nothing and the defaults silently win.
"""

from __future__ import annotations

import pytest

from formforge.registry import (
    LexicalMatcher,
    RegistryError,
    Route,
    Template,
    TemplateRegistry,
    parse_template,
)

MINIMAL = {
    "id": "test_widget",
    "category": "keychain",
    "display_name": "Test Widget",
    "description": "A widget for testing.",
    "language": "build123d",
    "param_schema": {
        "type": "object",
        "properties": {"width_mm": {"type": "number", "minimum": 10, "maximum": 100, "default": 50}},
    },
    "source": "from build123d import *\nWIDTH_MM = 50.0\nresult = Box(WIDTH_MM, WIDTH_MM, WIDTH_MM)",
}


class TestLoading:
    def test_parses_a_minimal_template(self):
        template = parse_template(MINIMAL)
        assert template.id == "test_widget"
        assert template.defaults() == {"width_mm": 50}

    def test_rejects_a_parameter_with_no_matching_constant(self):
        """The silent-drift failure.

        A schema parameter with no constant to bind to does nothing at all: the
        user moves a slider, the model does not change, and no layer reports a
        problem. Catching it at load time is the only place it is visible.
        """
        broken = dict(MINIMAL)
        broken["param_schema"] = {
            "type": "object",
            "properties": {"height_mm": {"type": "number", "default": 20}},
        }
        with pytest.raises(RegistryError) as excinfo:
            parse_template(broken)
        assert "HEIGHT_MM" in str(excinfo.value)

    def test_rejects_a_source_that_fails_the_static_gate(self):
        broken = dict(MINIMAL)
        broken["source"] = "import os\nWIDTH_MM = 50.0\nresult = None"
        with pytest.raises(RegistryError):
            parse_template(broken)

    @pytest.mark.parametrize("missing", ["id", "category", "display_name", "source"])
    def test_requires_the_essential_fields(self, missing):
        broken = {k: v for k, v in MINIMAL.items() if k != missing}
        with pytest.raises(RegistryError):
            parse_template(broken)


class TestParameters:
    @pytest.fixture
    def template(self) -> Template:
        return parse_template(MINIMAL)

    def test_accepts_values_in_range(self, template):
        assert template.validate_params({"width_mm": 60}) == []

    def test_rejects_values_out_of_range(self, template):
        problems = template.validate_params({"width_mm": 500})
        assert problems and "width_mm" in problems[0]

    def test_binds_values_into_the_source(self, template):
        source = template.render_source({"width_mm": 75})
        assert "WIDTH_MM = 75" in source

    def test_preconditions_reject_impossible_combinations(self):
        """A JSON Schema constrains each number alone; some rules span two."""
        spec = dict(MINIMAL)
        spec["param_schema"] = {
            "type": "object",
            "properties": {
                "width_mm": {"type": "number", "minimum": 10, "maximum": 100, "default": 50},
                "hole_d_mm": {"type": "number", "minimum": 1, "maximum": 90, "default": 5},
            },
        }
        spec["source"] = (
            "from build123d import *\nWIDTH_MM = 50.0\nHOLE_D_MM = 5.0\n"
            "result = Box(WIDTH_MM, WIDTH_MM, WIDTH_MM)"
        )
        spec["preconditions"] = ["hole_d_mm + 6 <= width_mm"]
        template = parse_template(spec)

        assert template.validate_params({"width_mm": 50, "hole_d_mm": 5}) == []
        problems = template.validate_params({"width_mm": 20, "hole_d_mm": 80})
        assert problems and "hole_d_mm + 6 <= width_mm" in problems[0]


class TestMatching:
    def test_the_shipped_registry_loads_cleanly(self, registry):
        assert len(registry) >= 10
        assert set(registry.categories()) >= {
            "keychain", "planter", "organizer", "box", "hook", "wall_decor"
        }

    def test_every_shipped_template_has_valid_defaults(self, registry):
        """A template whose own defaults are invalid is broken by construction."""
        for template in registry.all():
            assert template.validate_params(template.defaults()) == [], template.id

    def test_every_shipped_template_declares_its_print_status(self, registry):
        """'It validates' and 'it prints' are different claims.

        Every template must state which one it has earned. Nothing in this
        repository has been physically printed, so an unqualified "print tested"
        badge anywhere would be a fabricated record.
        """
        for template in registry.all():
            assert template.tested is not None, template.id
            assert template.tested.status in {"untested", "passed", "failed"}, template.id
            assert template.tested.rationale, template.id

    def test_no_template_claims_a_print_test_it_has_not_had(self, registry):
        for template in registry.all():
            if template.tested.passed:
                assert template.tested.date, (
                    f"{template.id} claims a passed print test with no date"
                )

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("gridfinity bin for my drawer", "box_gridfinity_bin"),
            ("a pen cup for my desk", "organizer_pen_cup"),
            ("wall planter for a plant", "planter_halfmoon_wall"),
            ("keychain with my name on it", "keychain_text_tag"),
            ("bottle opener keyring", "keychain_bottle_opener"),
            ("a hook for my keys by the door", "hook_wall_j"),
        ],
    )
    def test_finds_the_right_template(self, registry, query, expected):
        match = registry.best_match(query)
        assert match is not None
        assert match.template.id == expected, f"{query!r} matched {match.template.id}"

    def test_a_strong_match_routes_to_the_template_path(self, registry):
        route, match = registry.route("gridfinity bin")
        assert route is Route.TEMPLATE
        assert match.score >= registry.matcher.thresholds[0]

    def test_an_unrelated_request_routes_to_freeform(self, registry):
        route, _ = registry.route("a articulated dragon figurine with moving jaws")
        assert route is Route.FREEFORM

    def test_idf_weighting_beats_raw_overlap(self, registry):
        """'wall' appears in a third of the registry; 'gridfinity' identifies one.

        Without inverse-document-frequency weighting they would count equally,
        and every query containing a common word would match everything.
        """
        matcher = registry.matcher
        assert isinstance(matcher, LexicalMatcher)
        distinctive = matcher.score("gridfinity", registry.get("box_gridfinity_bin"))
        common = matcher.score("wall", registry.get("box_gridfinity_bin"))
        assert distinctive > common

    def test_category_filters_the_search(self, registry):
        matches = registry.search("holder", category="planter")
        assert all(m.template.category == "planter" for m in matches)

    def test_unknown_template_names_the_alternatives(self, registry):
        with pytest.raises(KeyError) as excinfo:
            registry.get("no_such_template")
        assert "keychain_text_tag" in str(excinfo.value)
