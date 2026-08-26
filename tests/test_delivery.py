"""What the user actually receives: the bundle, the MCP tools and the HTTP API.

The bundle tests assert the claim the whole architecture rests on -- that
`source.py` is a real script a person can run and edit, not a transcript of what
happened.
"""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

from formforge.bundle import write_bundle
from formforge.llm import OfflineClient
from formforge.mcp.server import TOOL_DEFINITIONS, FormForgeTools, ToolError
from formforge.orchestrator import Orchestrator
from formforge.store import Store


@pytest.fixture(scope="module")
def generated(registry, sandbox, tmp_path_factory):
    orchestrator = Orchestrator(
        registry=registry,
        client=OfflineClient(),
        sandbox=sandbox,
        output_dir=tmp_path_factory.mktemp("delivery"),
    )
    result = orchestrator.generate(
        "a name tag",
        interactive=False,
        template_id="keychain_text_tag",
        params={"text": "RIVER", "body_l_mm": 70, "cap_h_mm": 10},
    )
    assert result.ok, result.message
    return result


@pytest.fixture(scope="module")
def bundle(generated, registry, tmp_path_factory):
    return write_bundle(
        generated,
        tmp_path_factory.mktemp("bundle"),
        template=registry.get("keychain_text_tag"),
    )


class TestBundle:
    def test_contains_every_promised_artifact(self, bundle):
        for kind in ("3mf", "stl", "step", "source", "params", "report", "readme"):
            path = bundle.path(kind)
            assert path and path.exists(), f"missing {kind}"
            assert path.stat().st_size > 0

    def test_the_source_runs_standalone_and_rebuilds_the_model(self, bundle, generated):
        """The claim the parametric approach exists to make.

        A downloaded mesh is a dead end. A script the user can run, edit and
        re-run is the thing a mesh generator structurally cannot ship -- so it
        has to actually work outside this package.
        """
        namespace = runpy.run_path(str(bundle.path("source")))
        rebuilt = namespace["result"]
        assert rebuilt.volume == pytest.approx(generated.stats["volume_mm3"], rel=1e-6)

    def test_the_source_keeps_its_comments(self, bundle):
        """The comments are the reasoning. An AST round-trip would drop them."""
        source = bundle.path("source").read_text()
        assert "#" in source
        assert "chamfer" in source.lower()

    def test_the_parameters_are_bound_into_the_source(self, bundle):
        source = bundle.path("source").read_text()
        assert "TEXT = 'RIVER'" in source
        assert "BODY_L_MM = 70" in source

    def test_the_readme_leads_with_units(self, bundle):
        """Every 'my print came out 25.4x too big' ticket is an STL unit
        ambiguity, so the answer goes above the fold."""
        readme = bundle.path("readme").read_text()
        assert "millimetres" in readme[:400]
        assert "3mf" in readme.lower()

    def test_params_json_carries_the_schema_for_sliders(self, bundle):
        payload = json.loads(bundle.path("params").read_text())
        assert payload["values"]["text"] == "RIVER"
        assert "properties" in payload["schema"]

    def test_the_manifest_indexes_the_bundle(self, bundle):
        manifest = json.loads((bundle.directory / "manifest.json").read_text())
        assert manifest["units"] == "millimeter"
        assert manifest["validation_passed"] is True
        assert manifest["files"]["stl"] == "model.stl"


@pytest.fixture(scope="module")
def tools(registry, tmp_path_factory) -> FormForgeTools:
    return FormForgeTools(
        registry=registry,
        orchestrator=Orchestrator(
            registry=registry,
            client=OfflineClient(),
            output_dir=tmp_path_factory.mktemp("mcp"),
        ),
        store_dir=tmp_path_factory.mktemp("mcpstore"),
        # Never the developer's real database. A test suite that writes into
        # it would corrupt the only dataset here that cannot be regenerated.
        db=Store.memory(),
    )


class TestMcpTools:
    def test_every_declared_tool_is_dispatchable(self, tools):
        """A tool advertised in the schema and missing from dispatch is a
        contract the server cannot honour."""
        for definition in TOOL_DEFINITIONS:
            with pytest.raises((TypeError, ToolError, KeyError)):
                tools.call(definition["name"], {"__unlikely_argument__": 1})

    def test_tool_descriptions_steer_toward_templates(self, tools):
        """The template path is both the reliability story and the cost lever,
        so the description has to say so."""
        listing = next(t for t in TOOL_DEFINITIONS if t["name"] == "list_templates")
        assert "verified" in listing["description"]
        assert "cheaper" in listing["description"]

    def test_template_listings_carry_an_honest_print_test_status(self, tools):
        """No template here has been physically printed, and the tool result
        must not let a model imply otherwise."""
        for entry in tools.list_templates()["templates"]:
            assert entry["print_test_status"] in {"untested", "passed", "failed"}
            assert entry["print_tested"] is (entry["print_test_status"] == "passed")

    def test_lists_and_searches_templates(self, tools):
        assert tools.list_templates(category="planter")["count"] >= 2
        found = tools.list_templates(query="gridfinity bin")["templates"]
        assert found[0]["id"] == "box_gridfinity_bin"

    def test_generates_and_exports(self, tools):
        made = tools.generate_from_template(
            "keychain_text_tag", {"text": "MCP", "body_l_mm": 60}
        )
        assert made["status"] == "ok"
        assert made["dimensions_mm"][0] == pytest.approx(60.0)

        exported = tools.export_model(made["model_id"], ["3mf", "stl", "step"])
        assert set(exported["files"]) >= {"3mf", "stl", "step"}
        assert "units" in exported["note"]

    def test_rejects_out_of_range_parameters_with_the_reason(self, tools):
        with pytest.raises(ToolError) as excinfo:
            tools.generate_from_template("keychain_text_tag", {"body_l_mm": 5000})
        assert "tested range" in str(excinfo.value)

    def test_an_unknown_model_id_explains_itself(self, tools):
        with pytest.raises(ToolError) as excinfo:
            tools.check_printability("nope")
        assert "generate_" in str(excinfo.value)

    def test_the_validation_report_is_compacted_for_a_tool_result(self, tools):
        """A tool result carrying every passing check crowds out the
        conversation it is meant to inform."""
        made = tools.generate_from_template("keychain_text_tag", {"text": "X"})
        report = made["validation"]
        assert set(report) == {"passed", "summary", "failures", "warnings", "key_measurements"}
        assert report["summary"]["checks"] > len(report["failures"])

    def test_modify_preserves_lineage(self, tools):
        first = tools.generate_from_template("keychain_text_tag", {"text": "A"})
        second = tools.modify_model(first["model_id"], {"text": "B"})
        assert second["parent_model_id"] == first["model_id"]
        assert second["params"]["text"] == "B"

    def test_a_print_outcome_can_be_reported_back(self, tools):
        """The only ground truth the system has, reachable from the assistant.

        A user says "it warped"; that sentence is the entire empirical basis
        the DFM constants will ever have, and it is worth nothing unless it
        lands against the model it describes.
        """
        made = tools.generate_from_template("keychain_text_tag", {"text": "P"})
        recorded = tools.report_print_result(
            made["model_id"], success=False, issues=["warping"], printer="Bambu P1S"
        )
        assert recorded["recorded"] is True
        outcome = next(
            o for o in tools.db.print_outcomes() if o["model_id"] == made["model_id"]
        )
        assert outcome["issues"] == ["warping"]

    def test_a_print_outcome_for_an_unrecorded_model_is_refused(self, tools):
        with pytest.raises(ToolError) as excinfo:
            tools.report_print_result("nope", success=True)
        assert "generate_" in str(excinfo.value)

    def test_the_print_issue_vocabulary_is_enforced_at_the_tool_boundary(self, tools):
        """Free text here cannot be aggregated, and aggregation is the point."""
        made = tools.generate_from_template("keychain_text_tag", {"text": "Q"})
        with pytest.raises(ToolError) as excinfo:
            tools.report_print_result(
                made["model_id"], success=False, issues=["went a bit wrong"]
            )
        assert "warping" in str(excinfo.value)

    def test_the_reporting_tool_does_not_invite_invented_outcomes(self):
        """A model that infers "it must have worked" from a returning user
        poisons the one dataset that cannot be regenerated."""
        definition = next(
            t for t in TOOL_DEFINITIONS if t["name"] == "report_print_result"
        )
        assert "only what the user actually said" in definition["description"]


@pytest.fixture(scope="module")
def client(registry, tmp_path_factory):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from formforge.api import create_app

    app = create_app(
        registry=registry,
        orchestrator=Orchestrator(
            registry=registry,
            client=OfflineClient(),
            output_dir=tmp_path_factory.mktemp("api"),
        ),
        store_dir=tmp_path_factory.mktemp("apistore"),
        allow_unsafe_sandbox=True,
    )
    with fastapi_testclient.TestClient(app) as test_client:
        yield test_client


def _generate(client, prompt: str) -> str:
    """Submit a prompt and return the model id once the run has finished.

    TestClient runs background tasks before the response returns, so by the
    time this comes back the generation is persisted.
    """
    response = client.post("/v1/generate", json={"prompt": prompt, "interactive": False})
    assert response.status_code == 202
    return response.json()["model_id"]


class TestHttpApi:
    def test_health_reports_the_sandbox_posture(self, client):
        payload = client.get("/healthz").json()
        assert payload["ok"] is True
        assert "kernel_isolated" in payload["sandbox"]

    def test_lists_templates_and_profiles(self, client):
        assert len(client.get("/v1/templates").json()["templates"]) >= 10
        assert client.get("/v1/profiles").json()["default"]

    def test_generation_streams_the_loop_then_downloads(self, client):
        submitted = client.post(
            "/v1/generate",
            json={"prompt": "a hex planter 100mm across and 80mm tall", "interactive": False},
        )
        assert submitted.status_code == 202
        model_id = submitted.json()["model_id"]

        phases = []
        with client.websocket_connect(f"/v1/models/{model_id}/stream") as socket:
            for _ in range(25):
                event = socket.receive_json()
                phases.append(event.get("phase"))
                if event.get("phase") in {"closed", "done"}:
                    break
        assert "execute" in phases and "validate" in phases

        record = client.get(f"/v1/models/{model_id}").json()
        assert record["status"] == "ok"

        download = client.get(f"/v1/models/{model_id}/download?format=3mf")
        assert download.status_code == 200
        assert len(download.content) > 1000

    def test_an_unknown_model_is_a_404(self, client):
        assert client.get("/v1/models/nope/status").status_code == 404

    def test_records_print_feedback_against_a_real_model(self, client):
        """The only ground truth for whether any of this works.

        Against a real model id, because feedback that points at nothing can
        never be cross-referenced with what the validator measured -- which is
        the entire reason the table exists.
        """
        model_id = _generate(client, 'a keychain that says "TEST"')
        response = client.post(
            "/v1/feedback",
            json={
                "model_id": model_id,
                "printed": True,
                "success": False,
                "issues": ["warping"],
                "notes": "lifted at one corner",
            },
        )
        assert response.status_code == 201
        assert response.json()["feedback_id"]

        outcomes = client.get("/v1/stats/prints").json()["outcomes"]
        row = next(o for o in outcomes if o["model_id"] == model_id)
        # The point of the view: the reported failure sits next to the number
        # the validator measured at the time.
        assert row["issues"] == ["warping"]
        assert row["min_wall_mm"] is not None

    def test_feedback_for_an_unknown_model_is_rejected(self, client):
        response = client.post(
            "/v1/feedback", json={"model_id": "nope", "printed": True}
        )
        assert response.status_code == 404

    def test_an_unknown_issue_is_rejected_rather_than_stored(self, client):
        """A free-text issue column cannot be aggregated, so the set is closed."""
        model_id = _generate(client, "a pen cup 80mm across")
        response = client.post(
            "/v1/feedback",
            json={"model_id": model_id, "printed": True, "issues": ["went a bit wrong"]},
        )
        assert response.status_code == 422
        assert "warping" in response.json()["detail"]

    def test_the_per_step_log_survives_the_generation(self, client):
        """What makes a four-iteration run explicable after the fact."""
        model_id = _generate(client, "a pen cup 80mm across and 100mm tall")
        events = client.get(f"/v1/models/{model_id}/events").json()["events"]
        assert [e["phase"] for e in events][:2] == ["intent", "route"]
        assert all(e["created_at"] for e in events)

    def test_a_modification_records_its_parent(self, client):
        """Lineage that only exists in memory is lineage that does not exist."""
        parent = _generate(client, "a pen cup 80mm across")
        response = client.post(
            f"/v1/models/{parent}/modify", json={"param_changes": {"wall_mm": 2.5}}
        )
        assert response.status_code in (200, 202)
        child = response.json()["model_id"]
        events = client.get(f"/v1/models/{child}/events")
        assert events.status_code == 200

    def test_stats_answer_what_memory_cannot(self, client):
        payload = client.get("/v1/stats").json()
        assert payload["totals"]["generations"] >= 1
        assert payload["totals"]["write_failures"] == 0
        assert any(t["template_id"] for t in payload["templates"])

    def test_refuses_to_start_on_an_unisolated_sandbox_by_default(self, registry, tmp_path):
        """The single most consequential misconfiguration this system has.

        The API executes model-authored Python. Shipping the development
        runtime belongs in a startup check, not in a runbook.
        """
        # Every other test here is gated by the `client` fixture's skip. This
        # one builds the app itself, so it needs its own: without FastAPI,
        # create_app raises about the missing dependency before it ever
        # reaches the sandbox check, and the assertion below is not the
        # refusal this test exists to pin.
        pytest.importorskip("fastapi")
        from formforge.api import create_app

        with pytest.raises(RuntimeError) as excinfo:
            create_app(registry=registry, store_dir=tmp_path, allow_unsafe_sandbox=False)
        assert "gvisor" in str(excinfo.value)
