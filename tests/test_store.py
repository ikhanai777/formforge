"""The persistence layer (spec section 11).

These tables exist to be read months from now, and none of them can be
backfilled -- so what is worth testing is not that a row round-trips, but that
the collection keeps happening under the conditions where it would quietly
stop: a failed generation, a refusal, a telemetry write that itself fails.
"""

from __future__ import annotations

import json

import pytest

from formforge.orchestrator.loop import GenerationResult, LoopEvent
from formforge.store import PRINT_ISSUES, Store


def _result(model_id: str = "m1", status: str = "ok", **kwargs) -> GenerationResult:
    result = GenerationResult(
        model_id=model_id,
        status=status,
        prompt=kwargs.pop("prompt", "a keychain"),
        template_id=kwargs.pop("template_id", "keychain_text_tag"),
        route=kwargs.pop("route", "template"),
        iterations=kwargs.pop("iterations", 1),
    )
    result.stats = kwargs.pop("stats", {"bbox_mm": [70, 24, 4], "triangles": 2288})
    result.validation = kwargs.pop(
        "validation", {"measurements": {"min_wall_mm": 1.9}, "warnings": []}
    )
    result.events = kwargs.pop("events", [])
    for key, value in kwargs.items():
        setattr(result, key, value)
    return result


@pytest.fixture
def store() -> Store:
    with Store.memory() as db:
        yield db


class TestRecording:
    def test_a_generation_and_its_steps_are_stored_together(self, store):
        store.record_generation(
            _result(
                events=[
                    LoopEvent(1, "intent", True, "parsed"),
                    LoopEvent(2, "execute", True, "built", duration_ms=1200),
                ]
            )
        )
        record = store.get_model("m1")
        assert record["template_id"] == "keychain_text_tag"
        # JSON columns come back as objects, not as strings to parse again.
        assert record["bbox_mm"] == [70, 24, 4]
        assert [e["phase"] for e in store.events_for("m1")] == ["intent", "execute"]

    @pytest.mark.parametrize("status", ["failed", "refused", "needs_clarification"])
    def test_runs_that_did_not_succeed_are_recorded_too(self, store, status):
        """A store holding only the successes cannot answer anything useful."""
        store.record_generation(_result(model_id=status, status=status))
        assert store.get_model(status)["status"] == status

    def test_an_unknown_status_is_stored_as_failed_rather_than_lost(self, store):
        """The CHECK constraint must not be a way to drop a row."""
        store.record_generation(_result(status="exploded"))
        assert store.get_model("m1")["status"] == "failed"
        assert store.write_failures == 0

    def test_a_failing_step_carries_the_class_of_failure(self, store):
        """Grouping failures is the whole point of the column."""
        store.record_generation(
            _result(
                status="failed",
                events=[
                    LoopEvent(1, "execute", False, "boom", payload={"error_class": "Timeout"}),
                    LoopEvent(2, "validate", False, "thin", payload={
                        "failures": [{"id": "printability.wall_thickness"}]
                    }),
                    LoopEvent(3, "render", False, "no views"),
                ],
            )
        )
        assert [e["error_class"] for e in store.events_for("m1")] == [
            "Timeout",
            "printability.wall_thickness",
            "render_failed",  # never unclassified, even with an empty payload
        ]

    def test_a_payload_that_will_not_serialise_does_not_lose_the_row(self, store):
        """Telemetry containing one exotic object must not cost the whole step."""

        class Opaque:
            pass

        store.record_generation(
            _result(events=[LoopEvent(1, "codegen", True, "ok", payload={"o": Opaque()})])
        )
        assert len(store.events_for("m1")) == 1

    def test_a_write_failure_is_counted_rather_than_raised(self, store):
        """A generation the user is waiting on must not fail over telemetry."""
        store.close()
        store.record_generation(_result())
        assert store.write_failures == 1


    def test_remix_lineage_is_recorded(self, store):
        """A modification is a new model with a parent, never an edit in place.

        The original may already have been downloaded and printed.
        """
        store.record_generation(_result(model_id="parent"))
        store.record_generation(_result(model_id="child"), parent_id="parent")
        assert store.get_model("child")["parent_id"] == "parent"

    def test_an_unrecorded_parent_costs_the_lineage_not_the_row(self, store):
        """The generation is the evidence; the edge between two is convenience."""
        store.record_generation(_result(model_id="orphan"), parent_id="never_stored")
        record = store.get_model("orphan")
        assert record is not None
        assert record["parent_id"] is None

    def test_re_recording_does_not_duplicate_the_steps(self, store):
        events = [LoopEvent(1, "intent", True, "parsed")]
        store.record_generation(_result(events=events))
        store.record_generation(_result(events=events))
        assert len(store.events_for("m1")) == 1


class TestFeedback:
    def test_feedback_joins_to_what_the_validator_measured(self, store):
        """The whole reason the row points at a model.

        A print reported as weak is only useful next to the wall thickness that
        was measured at the time; that join is what eventually turns the DFM
        constants from convention into evidence.
        """
        store.record_generation(_result())
        store.record_feedback(
            {"model_id": "m1", "printed": True, "success": False, "issues": ["weak"]}
        )
        outcome = store.print_outcomes()[0]
        assert outcome["issues"] == ["weak"]
        assert outcome["min_wall_mm"] == 1.9
        assert outcome["template_id"] == "keychain_text_tag"

    def test_feedback_for_a_model_that_does_not_exist_is_refused(self, store):
        """Unlike telemetry, this one fails loudly -- see the module docstring."""
        with pytest.raises(Exception):
            store.record_feedback({"model_id": "ghost", "printed": True})

    def test_the_issue_vocabulary_is_closed(self):
        """Free text cannot be aggregated, and aggregation is the point."""
        assert "warping" in PRINT_ISSUES
        assert "went a bit wrong" not in PRINT_ISSUES

    def test_issues_round_trip_as_a_list_not_a_string(self, store):
        """Stored as JSON so a row moves to the Postgres text[] unchanged."""
        store.record_generation(_result())
        store.record_feedback(
            {"model_id": "m1", "printed": True, "success": True,
             "issues": ["warping", "poor_adhesion"]}
        )
        assert store.print_outcomes()[0]["issues"] == ["warping", "poor_adhesion"]


class TestViews:
    def test_template_health_separates_the_working_from_the_failing(self, store):
        store.record_generation(_result(model_id="a", status="ok"))
        store.record_generation(_result(model_id="b", status="failed"))
        store.record_generation(
            _result(model_id="c", status="ok", template_id="keychain_bottle_opener")
        )
        health = {row["template_id"]: row for row in store.template_health()}
        assert health["keychain_text_tag"]["generations"] == 2
        assert health["keychain_text_tag"]["success_rate"] == pytest.approx(0.5)
        assert health["keychain_bottle_opener"]["success_rate"] == pytest.approx(1.0)

    def test_failure_classes_are_ranked_by_how_often_they_happen(self, store):
        """So the failures being fixed are the ones that actually occur."""
        for i in range(3):
            store.record_generation(
                _result(
                    model_id=f"t{i}",
                    status="failed",
                    events=[LoopEvent(1, "execute", False, "", payload={"error_class": "Timeout"})],
                )
            )
        store.record_generation(
            _result(
                model_id="x",
                status="failed",
                events=[LoopEvent(1, "execute", False, "", payload={"error_class": "OOM"})],
            )
        )
        classes = store.failure_classes()
        assert [c["error_class"] for c in classes] == ["Timeout", "OOM"]
        assert classes[0]["occurrences"] == 3

    def test_totals_report_zero_rather_than_null_when_empty(self, store):
        """A blank where a zero belongs reads as broken, not as 'nothing yet'."""
        totals = store.totals()
        assert totals["generations"] == 0
        assert totals["succeeded"] == 0
        assert totals["cost_usd"] == 0.0


class TestPolicyEvents:
    def test_a_refusal_is_recorded_against_the_prompt_not_the_model(self, store):
        """A user probing the classifier looks like nothing when filed per model."""
        store.record_policy_event(
            "a pikachu keychain", "refuse", category="ip", matched=["pokemon"]
        )
        rows = store._conn.execute("SELECT * FROM policy_events").fetchall()
        assert rows[0]["decision"] == "refuse"
        assert json.loads(rows[0]["matched"]) == ["pokemon"]


class TestSchemaParity:
    """The Postgres schema in docs/schema.sql stays the target.

    SQLite is where collection happens now, so the two must not drift: a column
    that exists in one and not the other turns the migration into a rewrite.
    """

    def test_every_table_carries_the_columns_the_postgres_schema_declares(self, store):
        from pathlib import Path
        import re

        sql = Path("docs/schema.sql").read_text()
        for table in ("models", "generation_events", "print_feedback", "policy_events"):
            block = re.search(
                rf"CREATE TABLE {table} \((.*?)\n\);", sql, re.DOTALL
            )
            assert block, f"{table} not found in docs/schema.sql"
            # A column declaration starts at a fixed indent with an
            # identifier; anything else on its own line is a constraint, a
            # comment, or the wrapped tail of one.
            declared = {
                match.group(1)
                for match in (
                    re.match(r"    ([a-z_]+)\s+\S", line) for line in block.group(1).splitlines()
                )
                if match and match.group(1) not in {"check", "foreign", "primary", "constraint"}
            }
            # The embedding column is the one deliberate omission; SQLite should
            # not pretend to do vector search.
            declared.discard("embedding")
            actual = {
                row["name"]
                for row in store._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            assert declared <= actual, f"{table} is missing {sorted(declared - actual)}"
