"""The agent loop end to end, and the intent parsing that feeds it.

These run against the offline client, which is the point: the template path has
to work without a model, and that path is where most traffic should go.
"""

from __future__ import annotations

import pytest

from formforge.llm import OfflineClient
from formforge.orchestrator import Orchestrator
from formforge.orchestrator.intent import parse_heuristically, to_mm


class TestIntentParsing:
    @pytest.mark.parametrize(
        "prompt,category",
        [
            ("a keychain with my name on it", "keychain"),
            ("a wall planter for my herbs", "planter"),
            ("a pen cup for my desk", "organizer"),
            ("a hook for hanging keys", "hook"),
            ("a gridfinity bin", "box"),
            ("a monogram wall sign", "wall_decor"),
        ],
    )
    def test_detects_the_category(self, prompt, category):
        assert parse_heuristically(prompt).category == category

    def test_longest_keyword_wins(self):
        """'planter box' is a planter, not a box."""
        assert parse_heuristically("a planter box for the windowsill").category == "planter"

    @pytest.mark.parametrize(
        "value,unit,expected",
        [(4, "inch", 101.6), (10, "cm", 100.0), (60, "mm", 60.0), (2.5, '"', 63.5)],
    )
    def test_converts_units_to_millimetres(self, value, unit, expected):
        assert to_mm(value, unit) == pytest.approx(expected)

    def test_reads_dimensions_with_axis_words(self):
        intent = parse_heuristically("a planter 140mm wide and 90mm tall")
        assert intent.dimensions["width_mm"] == pytest.approx(140.0)
        assert intent.dimensions["height_mm"] == pytest.approx(90.0)

    def test_reads_a_dimension_triple(self):
        intent = parse_heuristically("a tray 150 x 100 x 45 mm")
        assert intent.dimensions["length_mm"] == pytest.approx(150.0)
        assert intent.dimensions["width_mm"] == pytest.approx(100.0)
        assert intent.dimensions["height_mm"] == pytest.approx(45.0)

    def test_understands_a_pot_size_in_inches(self):
        intent = parse_heuristically("a hex planter for a 4 inch pot")
        assert intent.dimensions["width_mm"] == pytest.approx(101.6, abs=0.1)

    @pytest.mark.parametrize(
        "prompt,text",
        [
            ('a keychain that says "RIVER"', "RIVER"),
            ("a name tag with the name Alice", "Alice"),
            ("a luggage tag reading SMITH", "SMITH"),
        ],
    )
    def test_extracts_text_for_the_model(self, prompt, text):
        assert parse_heuristically(prompt).text_content == text

    def test_detects_the_mount_type(self):
        assert parse_heuristically("a planter that hangs on a screw").mount_type == "keyhole_slot"
        assert parse_heuristically("a hook with adhesive backing").mount_type == "adhesive_pad"

    def test_detects_constraints(self):
        constraints = parse_heuristically("a box that prints with no supports").constraints
        assert constraints["avoid_supports"] is True

    def test_asks_only_about_missing_functional_dimensions(self):
        """Never about style. A user asked their aesthetic feels interrogated."""
        vague = parse_heuristically("a wall planter")
        assert vague.needs_clarification
        assert all("mm" == q["unit"] for q in vague.clarifications)
        assert not any(
            word in q["question"].lower()
            for q in vague.clarifications
            for word in ("style", "aesthetic", "colour", "color", "look")
        )

    def test_does_not_ask_when_the_dimensions_are_given(self):
        assert not parse_heuristically("a wall planter 120mm wide").needs_clarification

    def test_the_search_query_drops_request_framing(self):
        intent = parse_heuristically("can you make me a hexagonal plant pot please")
        assert "make" not in intent.search_query().lower()
        assert "hexagonal" in intent.search_query().lower()


class TestOfflineClient:
    def test_reports_itself_unavailable(self):
        assert OfflineClient().available is False

    def test_explains_what_is_missing_rather_than_failing_obscurely(self):
        from formforge.llm import LLMError

        with pytest.raises(LLMError) as excinfo:
            OfflineClient().complete(system="", messages=[], purpose="intent parsing")
        message = str(excinfo.value)
        assert "ANTHROPIC_API_KEY" in message
        assert "template path works" in message


@pytest.fixture(scope="module")
def orchestrator(registry, sandbox, tmp_path_factory) -> Orchestrator:
    return Orchestrator(
        registry=registry,
        client=OfflineClient(),
        sandbox=sandbox,
        output_dir=tmp_path_factory.mktemp("loop"),
    )


class TestLoop:
    def test_generates_from_a_description_with_no_model_available(self, orchestrator):
        result = orchestrator.generate(
            "a wall planter 140mm wide and 100mm tall with drainage",
            interactive=False,
        )
        assert result.ok, result.message
        assert result.template_id == "planter_halfmoon_wall"
        assert result.stats["bbox_mm"][0] == pytest.approx(140.0, abs=1.0)

    def test_emits_an_event_for_every_phase(self, orchestrator):
        events = []
        orchestrator.generate(
            "a pen cup 80mm square and 95mm tall",
            interactive=False,
            on_event=events.append,
        )
        phases = [e.phase for e in events]
        for expected in ("intent", "route", "codegen", "execute", "validate", "render"):
            assert expected in phases, f"no {expected} event: {phases}"

    def test_asks_rather_than_guessing_a_missing_dimension(self, orchestrator):
        result = orchestrator.generate("a drawer organizer", interactive=True)
        assert result.status == "needs_clarification"
        assert result.clarifications
        assert "Need one thing" in result.summary()

    def test_refuses_infringing_requests_before_generating(self, orchestrator):
        result = orchestrator.generate("a Pikachu keychain", interactive=False)
        assert result.status == "refused"
        assert result.artifacts == {}
        assert "copyright" in result.message or "trademark" in result.message

    def test_an_explicit_template_and_parameters_are_honoured(self, orchestrator):
        result = orchestrator.generate(
            "a name tag",
            interactive=False,
            template_id="keychain_text_tag",
            params={"text": "ALICE", "body_l_mm": 70},
        )
        assert result.ok, result.message
        assert result.params["text"] == "ALICE"
        assert result.stats["bbox_mm"][0] == pytest.approx(70.0)

    def test_a_template_that_fails_validation_stops_rather_than_retrying(
        self, registry, sandbox, tmp_path
    ):
        """Retrying a template cannot help: the same parameters build the same
        solid every time. Burning three more iterations on it would be pure waste.
        """
        orchestrator = Orchestrator(
            registry=registry,
            client=OfflineClient(),
            sandbox=sandbox,
            output_dir=tmp_path,
        )
        result = orchestrator.generate(
            "a gridfinity bin",
            interactive=False,
            template_id="box_gridfinity_bin",
            params={"grid_x": 1, "grid_y": 1, "units_z": 2, "wall_mm": 0.8},
        )
        assert result.iterations == 1

    def test_the_result_serialises(self, orchestrator):
        import json

        result = orchestrator.generate(
            "a hex planter 100mm across and 80mm tall", interactive=False
        )
        payload = json.loads(json.dumps(result.as_dict(), default=str))
        assert payload["status"] == "ok"
        assert payload["events"]
