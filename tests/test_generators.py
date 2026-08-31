"""The dataflow graph and the mushroom definition built on it.

The property that matters here is not "does it produce numbers" but "does it
produce numbers the geometry will accept". A generator that emits a parameter
set the template rejects has produced nothing: the variation never becomes a
model. So the bulk of these tests solve the definition across its whole input
space and hand every result to the template's own validator.
"""

from __future__ import annotations

import pytest

from formforge.generators import Definition, DefinitionError
from formforge.generators import mushroom as mush


@pytest.fixture(scope="module")
def template(registry):
    return registry.get(mush.TEMPLATE_ID)


class TestGraph:
    def test_solves_in_dependency_order(self):
        definition = Definition("t")
        definition.slider("a", 2.0)
        definition.component("a", name="double")(lambda a: a * 2)
        definition.component("double", name="plus_one")(lambda d: d + 1)
        solution = definition.solve(a=3.0)
        assert solution["double"] == 6.0
        assert solution["plus_one"] == 7.0

    def test_a_cycle_cannot_be_expressed(self):
        """Wiring in dependency order is what makes the graph acyclic.

        Grasshopper detects cycles at solve time and refuses. Here a component
        can only name values that already exist, so the refusal happens while
        the definition is being written instead.
        """
        definition = Definition("t")
        definition.slider("a", 1.0)
        with pytest.raises(DefinitionError, match="not defined yet"):
            definition.component("later", name="early")(lambda x: x)

    def test_rejects_a_duplicate_name(self):
        definition = Definition("t")
        definition.slider("a", 1.0)
        with pytest.raises(DefinitionError, match="already defined"):
            definition.slider("a", 2.0)

    def test_rejects_an_unknown_input(self):
        definition = Definition("t")
        definition.slider("a", 1.0)
        with pytest.raises(DefinitionError, match="no slider named"):
            definition.solve(b=2.0)

    def test_sliders_clamp_to_their_stops(self):
        definition = Definition("t")
        definition.slider("a", 5.0, low=0.0, high=10.0)
        definition.slider("mode", "one", choices=("one", "two"))
        solution = definition.solve(a=99.0, mode="nine")
        assert solution["a"] == 10.0
        assert solution["mode"] == "one"

    def test_explain_names_every_node(self):
        text = mush.DEFINITION.explain()
        for node in mush.DEFINITION.order():
            assert node in text


class TestMushroomParameters:
    """Every specimen the definition can emit has to be buildable."""

    @pytest.mark.parametrize("species", [*mush.species_names(), "mixed"])
    @pytest.mark.parametrize("variation", [0.0, 0.55, 1.0])
    def test_every_specimen_satisfies_the_template(self, template, species, variation):
        for seed in (0, 7, 123, 4242, 9999):
            params = mush.specimen(seed, species=species, variation=variation)
            problems = template.validate_params(params)
            assert not problems, f"{species} seed={seed} v={variation}: {problems}"

    def test_the_same_seed_is_the_same_mushroom(self):
        assert mush.specimen(11, species="parasol") == mush.specimen(11, species="parasol")

    def test_a_population_is_stable_as_it_grows(self):
        """Member three is member three whether you asked for four or forty.

        A population that renumbers itself when the count changes cannot be
        extended: the specimen already printed comes back as something else.
        """
        few = mush.variations(4, seed=5, species="mixed")
        many = mush.variations(40, seed=5, species="mixed")
        assert few == many[:4]

    def test_variation_zero_rebuilds_the_species(self):
        params = mush.specimen(999, species="bolete", variation=0.0)
        preset = mush.SPECIES["bolete"]
        assert params["cap_d_mm"] == pytest.approx(preset["cap_d_mm"], rel=0.02)
        assert params["cap_h_mm"] == pytest.approx(preset["cap_h_mm"], rel=0.02)
        assert params["underside"] == preset["underside"]

    def test_specimens_actually_differ(self):
        population = mush.variations(12, seed=3, species="mixed", variation=0.7)
        signatures = {tuple(sorted(p.items())) for p in population}
        assert len(signatures) == len(population)
        assert len({p["cap_d_mm"] for p in population}) > 6

    def test_species_keep_their_character(self):
        """A bolete stays a bolete: the categorical sliders are not jittered."""
        for seed in (1, 2, 3, 4, 5):
            params = mush.specimen(seed, species="bolete", variation=1.0)
            assert params["underside"] == "pores"
            assert params["ring_style"] == "none"
            assert params["wart_count"] == 0

    def test_a_negative_slider_survives_the_jitter(self):
        """A bolete's stem widens upward, which is a negative taper.

        Anything that floors the jitter at zero turns every bolete into a
        cylinder, and the parameter set still validates -- so nothing downstream
        would catch it.
        """
        for seed in (1, 2, 3, 4, 5, 6):
            assert mush.specimen(seed, species="bolete", variation=1.0)["stem_taper"] < 0

    def test_parts_stay_in_proportion(self):
        """The dependent sliders follow the cap rather than wander on their own."""
        for seed in (0, 17, 555, 8123):
            params = mush.specimen(seed, species="mixed", variation=1.0)
            assert params["stem_d_mm"] < params["cap_d_mm"] * 0.62
            if params["underside"] == "gills":
                assert 10 <= params["gill_count"] <= 18
                assert params["gill_depth_mm"] <= params["cap_flesh_mm"] + 3

    def test_a_pin_is_carried_through_the_whole_population(self, template):
        population = mush.variations(6, seed=8, species="mixed", overrides={"cap_d_mm": 90})
        for params in population:
            assert params["cap_d_mm"] == 90
            assert not template.validate_params(params)

    def test_a_pin_drives_what_depends_on_it(self):
        """Pins land before the proportion node, not after it.

        A pinned 100 mm cap on a stem sized for a 62 mm one is not a mushroom,
        so the pin has to be visible to everything downstream of it.
        """
        small = mush.specimen(4, species="toadstool", overrides={"cap_d_mm": 40})
        large = mush.specimen(4, species="toadstool", overrides={"cap_d_mm": 100})
        assert large["stem_h_mm"] > small["stem_h_mm"] * 1.5
        assert large["wart_count"] > small["wart_count"]

    def test_detail_is_capped_on_a_big_cap(self):
        """Detail is bounded by what the sandbox will build, not by taste alone.

        Every gill and every wart is a solid in one boolean union, and each one
        costs more on a bigger cap. Left to scale with the circumference, a
        120 mm cap runs the geometry past the sandbox's CPU ceiling and the
        specimen never gets built at all.
        """
        small = mush.specimen(6, species="toadstool", overrides={"cap_d_mm": 60})
        huge = mush.specimen(6, species="toadstool", overrides={"cap_d_mm": 120})
        assert huge["gill_count"] <= small["gill_count"]
        assert huge["gill_count"] >= 10

    def test_an_unknown_species_is_refused(self):
        with pytest.raises(ValueError, match="unknown species"):
            mush.specimen(1, species="shiitake")

    def test_the_seed_reaches_the_geometry(self):
        """The scatter inside the model is driven by the same seed."""
        params = mush.specimen(4321, species="fly_agaric")
        assert params["seed"] == 4321

    def test_describe_says_what_it_is(self):
        text = mush.describe(mush.specimen(1, species="fly_agaric"))
        assert "mm cap" in text and "warts" in text


class TestMushroomTemplate:
    def test_the_template_is_registered_and_bindable(self, template):
        assert template.category == "nature"
        assert not template.validate_params(template.defaults())

    def test_every_generator_parameter_exists_in_the_schema(self, template):
        """The definition and the template cannot drift apart silently."""
        for species, preset in mush.SPECIES.items():
            unknown = set(preset) - set(template.properties)
            assert not unknown, f"{species} sets {unknown}, which the template cannot bind"
        for name in (*mush.JITTER, *mush.DERIVED, *mush.CATEGORICAL):
            assert name in template.properties, f"{name} is not a template parameter"

    def test_binding_reaches_every_constant(self, template):
        """A parameter that binds to nothing is a slider that does nothing."""
        from formforge import binding

        params = mush.specimen(77, species="parasol")
        result = binding.bind(template.source, params)
        assert result.ok, result.unbound

    @pytest.mark.slow
    def test_a_generated_specimen_builds_and_validates(self, template, sandbox):
        """One end-to-end pass: definition -> geometry -> STL -> DFM verdict.

        The range sweep in `formforge.eval.check_templates` is the thorough
        version; this is the one that runs with the rest of the suite.
        """
        from formforge.sandbox import ExecuteRequest
        from formforge.validation import validate

        params = mush.specimen(21, species="fly_agaric", variation=0.4)
        execution = sandbox.execute(
            ExecuteRequest(
                source=template.render_source(params),
                language=template.language,
                params=params,
                enforce_named_constants=False,
            )
        )
        assert execution.ok, execution.feedback()

        report = validate(
            execution.artifacts["stl"],
            category=template.category,
            params=params,
            template_invariants=template.invariants,
            expected_solids=template.expected_solids,
            brep_features=execution.stats.get("brep_features"),
        )
        assert report.passed, report.agent_feedback()
