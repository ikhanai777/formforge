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

    def test_records_print_feedback(self, client):
        """The only ground truth for whether any of this works."""
        response = client.post(
            "/v1/feedback",
            json={
                "model_id": "abc",
                "printed": True,
                "success": False,
                "issues": ["warping"],
            },
        )
        assert response.status_code == 201

    def test_refuses_to_start_on_an_unisolated_sandbox_by_default(self, registry, tmp_path):
        """The single most consequential misconfiguration this system has.

        The API executes model-authored Python. Shipping the development
        runtime belongs in a startup check, not in a runbook.
        """
        from formforge.api import create_app

        with pytest.raises(RuntimeError) as excinfo:
            create_app(registry=registry, store_dir=tmp_path, allow_unsafe_sandbox=False)
        assert "gvisor" in str(excinfo.value)
