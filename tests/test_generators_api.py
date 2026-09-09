"""The generators over HTTP.

Six generators existed from early on and were reachable only from the command
line. That was not a small gap: a generator is the thing that decides where a
template's sliders go, so `formforge vase --style amphora` is most of what the
product actually makes, and none of it could be reached from a browser.

What these tests hold to account is that exposing them did not create a second
build pipeline. A specimen goes through the same `engine.generate(template_id=,
params=)` call `modify` already used, becomes an ordinary model with its own id
and artifacts, and is charged the same one credit. If that ever forks, the
assertions about ownership and charging below stop lining up.

The batch rule, decided deliberately: **one credit per model**, and a batch
that costs more than the balance is refused *before* anything is queued. A
batch that ran until the credits ran out would leave somebody with an arbitrary
prefix of what they asked for and no clear account of why.
"""

from __future__ import annotations

import time

import pytest

from formforge.accounts import OfflineProvider
from formforge.generators import CATALOG

fastapi = pytest.importorskip("fastapi", reason="needs the `api` extra")
from fastapi.testclient import TestClient  # noqa: E402

from formforge.api.app import create_app  # noqa: E402

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def app(accounts, tmp_path, monkeypatch):
    monkeypatch.setenv("FORMFORGE_ALLOW_UNSAFE_SANDBOX", "1")
    import formforge.api.security as security

    monkeypatch.setattr(security, "COOKIE_INSECURE", True)
    return create_app(
        store_dir=tmp_path / "store",
        accounts=accounts,
        billing_provider=OfflineProvider(secret="s"),
        allow_unsafe_sandbox=True,
    )


@pytest.fixture
def client(app):
    with TestClient(app) as made:
        yield made


@pytest.fixture
def owner(client, app):
    response = client.post(
        "/v1/auth/signup", json={"email": "maker@example.com", "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    return app.state.formforge["accounts"].get_user_by_email("maker@example.com")


def settle(client, model_ids, seconds=180):
    """Wait for a batch to finish. The CAD kernel is genuinely slow."""
    deadline = time.time() + seconds
    states = {}
    while time.time() < deadline:
        states = {
            m: client.get(f"/v1/models/{m}").json().get("status") for m in model_ids
        }
        if all(s in ("ok", "failed") for s in states.values()):
            return states
        time.sleep(1)
    return states


class TestTheCatalogue:
    def test_every_generator_in_the_package_is_served(self, client):
        """The list is read from the package rather than transcribed, so a
        seventh generator appears here the day it is added."""
        served = {g["name"] for g in client.get("/v1/generators").json()["generators"]}
        assert served == {g.name for g in CATALOG}

    def test_each_one_names_its_own_variants_and_the_word_for_them(self, client):
        """A mushroom has species and a vase has styles. The domain's word is
        data, not something a client has to keep a lookup table for."""
        by_name = {
            g["name"]: g for g in client.get("/v1/generators").json()["generators"]
        }
        assert by_name["mushroom"]["variant_noun"] == "species"
        assert "fly_agaric" in by_name["mushroom"]["variants"]
        assert by_name["vase"]["variant_noun"] == "style"
        assert "amphora" in by_name["vase"]["variants"]

    def test_a_generator_carries_the_template_it_drives(self, client):
        detail = client.get("/v1/generators/vase").json()
        assert detail["template_id"] == "vessel_vase"
        # The template's own detail(), the same shape /v1/templates/{id}
        # returns, rather than a second description that could drift.
        assert detail["template"]["id"] == "vessel_vase"

    def test_an_unknown_generator_is_404(self, client):
        assert client.get("/v1/generators/teapot").status_code == 404

    def test_the_catalogue_needs_no_session(self, client):
        """Public for the same reason the template catalogue is: knowing a
        vase generator exists tells an attacker nothing."""
        assert client.get("/v1/generators").status_code == 200
        assert client.get("/v1/generators/vase").status_code == 200


class TestRunningOne:
    def test_anonymous_is_refused(self, client):
        assert client.post("/v1/generators/vase", json={"count": 1}).status_code == 401

    def test_an_unknown_variant_is_refused_by_name(self, client, owner):
        response = client.post(
            "/v1/generators/vase", json={"count": 1, "variant": "teapot"}
        )
        assert response.status_code == 422
        # The message names the word the domain uses and what is valid.
        assert "style" in response.json()["detail"]
        assert "amphora" in response.json()["detail"]

    def test_the_variant_is_optional_and_defaults(self, client, owner):
        response = client.post("/v1/generators/vase", json={"count": 1})
        assert response.status_code == 202
        vase = next(g for g in CATALOG if g.name == "vase")
        assert response.json()["style"] in vase.variants()

    def test_a_batch_queues_one_model_per_specimen(self, client, owner):
        response = client.post(
            "/v1/generators/vase",
            json={"count": 2, "variant": "amphora", "seed": 42},
        )
        assert response.status_code == 202
        body = response.json()
        assert body["requested"] == 2
        assert body["queued"] == 2
        ids = [s["model_id"] for s in body["specimens"]]
        assert len(set(ids)) == 2, "two specimens shared a model id"

    def test_the_seed_is_reported_per_specimen(self, client, owner):
        """A population is reproducible only if you can see which seed made
        which member."""
        body = client.post(
            "/v1/generators/vase", json={"count": 2, "variant": "classic", "seed": 9}
        ).json()
        seeds = [s["seed"] for s in body["specimens"]]
        assert len(set(seeds)) == 2, "every specimen got the same seed"


class TestCreditsAndOwnership:
    def test_a_batch_larger_than_the_balance_is_refused_before_anything_runs(
        self, client, owner
    ):
        """Not half-built. A partial batch is an arbitrary prefix of what was
        asked for, and no way to explain which part."""
        response = client.post("/v1/generators/vase", json={"count": 9})
        assert response.status_code == 402
        assert "9 credits" in response.json()["detail"]
        assert "3" in response.json()["detail"], "the message must say the balance"
        assert client.get("/v1/account/history").json()["models"] == []

    def test_an_empty_balance_is_refused(self, client, app, owner):
        accounts = app.state.formforge["accounts"]
        accounts.spend(owner["id"], accounts.balance(owner["id"]), idempotency_key="drain")
        assert client.post("/v1/generators/vase", json={"count": 1}).status_code == 402

    @pytest.mark.slow
    def test_one_credit_per_model_that_builds(self, client, owner):
        """The rule, end to end: a batch of two costs two."""
        before = client.get("/v1/auth/me").json()["credits"]
        body = client.post(
            "/v1/generators/vase", json={"count": 2, "variant": "classic", "seed": 3}
        ).json()
        ids = [s["model_id"] for s in body["specimens"] if s["status"] == "queued"]
        states = settle(client, ids)
        built = sum(1 for s in states.values() if s == "ok")
        assert built, f"nothing built: {states}"
        assert client.get("/v1/auth/me").json()["credits"] == before - built

    @pytest.mark.slow
    def test_a_specimen_is_an_ordinary_owned_model(self, client, owner):
        """The point of routing through the same build path: nothing about a
        generated model is special afterwards."""
        body = client.post(
            "/v1/generators/vase", json={"count": 1, "variant": "bottle", "seed": 5}
        ).json()
        model_id = body["specimens"][0]["model_id"]
        assert settle(client, [model_id])[model_id] == "ok"

        model = client.get(f"/v1/models/{model_id}").json()
        assert model["template_id"] == "vessel_vase"
        assert model["params"], "a generated model carries the params that made it"
        # It appears in the owner's history and can be downloaded like any other.
        listed = client.get("/v1/account/history").json()["models"]
        assert model_id in {m["model_id"] for m in listed}
        assert client.post(
            f"/v1/models/{model_id}/download-link?format=stl"
        ).status_code == 200

    @pytest.mark.slow
    def test_a_stranger_cannot_see_a_generated_model(self, client, owner):
        """Ownership is the ordinary rule, not a special case for batches."""
        body = client.post(
            "/v1/generators/vase", json={"count": 1, "variant": "bud", "seed": 11}
        ).json()
        model_id = body["specimens"][0]["model_id"]
        settle(client, [model_id])

        client.cookies.clear()
        client.post(
            "/v1/auth/signup",
            json={"email": "stranger@example.com", "password": "another-long-password"},
        )
        assert client.get(f"/v1/models/{model_id}").status_code == 404
        assert client.post(
            f"/v1/models/{model_id}/download-link?format=stl"
        ).status_code == 404


class TestTheFreeGatewayStillWorks:
    """No accounts means no credit check -- the same as `/v1/generate`."""

    @pytest.fixture
    def open_client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORMFORGE_ALLOW_UNSAFE_SANDBOX", "1")
        app = create_app(store_dir=tmp_path / "open", allow_unsafe_sandbox=True)
        with TestClient(app) as made:
            yield made

    def test_the_catalogue_is_served(self, open_client):
        assert open_client.get("/v1/generators").status_code == 200

    def test_a_batch_needs_no_session(self, open_client):
        response = open_client.post("/v1/generators/vase", json={"count": 1})
        assert response.status_code == 202


class TestBuildingATemplateFromParameters:
    """`POST /v1/templates/{id}/build` — the third way in.

    A prompt guesses, a generator samples, and this one is told. It is what
    the studio's sliders need: somebody who has just dialled a cap to 66 mm
    knows exactly what they want, and making them describe it in English and
    hope the parser agrees would be a worse product.
    """

    def test_anonymous_is_refused(self, client):
        assert client.post(
            "/v1/templates/nature_mushroom/build", json={"params": {}}
        ).status_code == 401

    def test_an_unknown_template_is_404(self, client, owner):
        assert client.post(
            "/v1/templates/nope/build", json={"params": {}}
        ).status_code == 404

    def test_a_parameter_outside_the_tested_range_is_refused_by_name(
        self, client, owner
    ):
        """Refused here, not four minutes into a build. The message is the
        template's own, so it names the bound rather than saying 'invalid'."""
        response = client.post(
            "/v1/templates/nature_mushroom/build", json={"params": {"cap_d_mm": 9999}}
        )
        assert response.status_code == 422
        assert "cap_d_mm" in response.json()["detail"]

    def test_an_empty_balance_is_refused(self, client, app, owner):
        accounts = app.state.formforge["accounts"]
        accounts.spend(owner["id"], accounts.balance(owner["id"]), idempotency_key="drain")
        assert client.post(
            "/v1/templates/nature_mushroom/build", json={"params": {}}
        ).status_code == 402

    def test_only_the_changed_parameters_need_sending(self, client, owner):
        """Merged against the template's defaults, so a client sends what it
        changed rather than restating thirty sliders."""
        assert client.post(
            "/v1/templates/nature_mushroom/build", json={"params": {"cap_d_mm": 66}}
        ).status_code == 202

    @pytest.mark.slow
    def test_the_parameters_sent_are_the_parameters_built(self, client, owner):
        """The whole promise of a slider."""
        wanted = {"cap_d_mm": 66, "cap_h_mm": 30, "stem_h_mm": 72, "wart_count": 22}
        job = client.post(
            "/v1/templates/nature_mushroom/build", json={"params": wanted}
        ).json()
        model_id = job["model_id"]
        assert settle(client, [model_id])[model_id] == "ok"

        model = client.get(f"/v1/models/{model_id}").json()
        assert model["template_id"] == "nature_mushroom"
        for key, value in wanted.items():
            assert model["params"][key] == value, f"{key} was not what was asked for"
        assert client.post(
            f"/v1/models/{model_id}/download-link?format=stl"
        ).status_code == 200


class TestTheStudioIsWiredToTheBuildEndpoint:
    """`web/studio.html` is the sliders. These check the join between it and
    the API, which no unit test of either side would catch.

    Not a browser test: the studio needs three.js from a CDN, and a headless
    run of it would be testing the network. What is checked here is the part
    that can rot silently -- a template renamed on one side of the join.
    """

    @pytest.fixture
    def studio(self):
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "web" / "studio.html").read_text()

    def test_it_posts_to_the_build_endpoint(self, studio):
        assert '"/v1/templates/" + encodeURIComponent(MODEL.templateId) + "/build"' in studio
        assert "body: { params: paramsOnly() }" in studio

    def test_every_template_it_names_is_a_real_one(self, studio, client):
        """The join. A template renamed in the registry and not in the studio
        is a build button that 404s, and nothing else would notice."""
        import re

        from formforge.registry import TemplateRegistry

        registry = TemplateRegistry.load(strict=False)
        named = set(re.findall(r'templateId:\s*"([^"]+)"', studio))
        assert named, "the studio names no templates; the pattern has drifted"
        for template_id in sorted(named):
            assert template_id in registry, (
                f"the studio's build button points at {template_id!r}, "
                "which the registry does not have"
            )

    def test_the_build_endpoint_accepts_each_of_them(self, studio, client, owner):
        """Defaults only -- enough to prove the route resolves and the
        template validates, without running six builds."""
        import re

        for template_id in sorted(set(re.findall(r'templateId:\s*"([^"]+)"', studio))):
            response = client.post(
                f"/v1/templates/{template_id}/build", json={"params": {}}
            )
            assert response.status_code in (202, 402), (
                f"{template_id}: {response.status_code} {response.text[:120]}"
            )

    def test_the_bridge_survives_the_viewer_failing(self, studio):
        """The build path needs no CDN and the viewer needs three.js, so they
        are separate scripts: a classic script stops at a throw, the next one
        still runs. Found by a container that could not reach cdnjs, which is
        also a train, an office proxy, and a bad afternoon."""
        bridge = studio.index("13. Building it for real")
        viewer = studio.index("THREE.WebGLRenderer")
        between = studio[viewer:bridge]
        assert "</script>" in between, (
            "the build bridge shares a <script> with the viewer, so a CDN "
            "failure would take both down"
        )
