"""The browser front end: what it serves, and what it must never serve.

The pages hold no data and make no decisions -- every byte on screen arrives
from the JSON API, which does the authorising. That is a strong claim, and
these tests are what keeps it from quietly becoming false:

* a page is the *same bytes* for everyone, signed in or not, and for any model
  id, so the shell cannot become an oracle for what exists;
* nothing served from `/static` or a page carries a secret;
* the model page's owner-only behaviour is the API's, unchanged -- the front
  end asks and renders, and a 404 stays a 404.

The rendering itself is not tested here. It is vanilla DOM code with no build
step, and a headless browser would be a large dependency to assert what the
manual pass already covers. What *is* tested is the contract between the shell
and the API, which is the part a refactor can break silently.
"""

from __future__ import annotations

import pytest

from formforge.accounts import OfflineProvider

fastapi = pytest.importorskip("fastapi", reason="needs the `api` extra")
from fastapi.testclient import TestClient  # noqa: E402

from formforge.api.app import create_app  # noqa: E402
from formforge.api.security import COOKIE_NAME  # noqa: E402
from formforge.api.web import ASSETS, PAGES  # noqa: E402

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def app(accounts, tmp_path, monkeypatch):
    monkeypatch.setenv("FORMFORGE_ALLOW_UNSAFE_SANDBOX", "1")
    import formforge.api.security as security

    monkeypatch.setattr(security, "COOKIE_INSECURE", True)
    return create_app(
        store_dir=tmp_path / "store",
        accounts=accounts,
        billing_provider=OfflineProvider(secret="local-secret"),
        allow_unsafe_sandbox=True,
    )


@pytest.fixture
def client(app):
    with TestClient(app) as made:
        yield made


def signup(client, email="owner@example.com", password=PASSWORD):
    response = client.post(
        "/v1/auth/signup", json={"email": email, "password": password}
    )
    assert response.status_code == 201, response.text
    return response


# Concrete URLs for the templated paths in PAGES.
URLS = [
    p.replace("{model_id}", "00000000-0000-0000-0000-000000000000") for p in PAGES
]


class TestThePagesAreServed:
    @pytest.mark.parametrize("url", URLS)
    def test_every_page_loads(self, client, url):
        response = client.get(url)
        assert response.status_code == 200, url
        assert response.headers["content-type"].startswith("text/html")

    @pytest.mark.parametrize("url", URLS)
    def test_every_page_is_a_complete_document(self, client, url):
        body = client.get(url).text
        assert body.lstrip().startswith("<!doctype html>"), url
        assert '<html lang="en">' in body
        assert "</html>" in body.strip()[-16:], f"{url} looks truncated"

    @pytest.mark.parametrize("url", URLS)
    def test_every_page_pulls_in_the_shared_assets(self, client, url):
        body = client.get(url).text
        assert '/static/app.css' in body, url
        assert '/static/app.js' in body, url

    def test_the_assets_are_served_with_the_right_type(self, client):
        for path, (_, media_type) in ASSETS.items():
            response = client.get(path)
            assert response.status_code == 200, path
            assert response.headers["content-type"].startswith(media_type.split(";")[0])
            assert response.content, path

    def test_an_unknown_page_is_still_a_404(self, client):
        """A catch-all would turn every typo into a blank page that looks like
        the app is broken."""
        assert client.get("/nope").status_code == 404


class TestThePagesCarryNoData:
    """The claim the whole design rests on."""

    @pytest.mark.parametrize("url", URLS)
    def test_a_page_is_identical_signed_in_and_out(self, client, url):
        anonymous = client.get(url).content
        signup(client)
        assert client.cookies.get(COOKIE_NAME)
        authenticated = client.get(url).content
        assert anonymous == authenticated, (
            f"{url} differs by session, so the shell is carrying account data"
        )

    def test_the_model_page_is_identical_for_any_id(self, client):
        """`/models/{model_id}` is declared `resource=None` in policies.py --
        it does not resolve the id, it ignores it. If that ever stops being
        true, the shell becomes an oracle for which model ids exist."""
        signup(client)
        real = client.post(
            "/v1/generate", json={"prompt": "a small test cube"}
        ).json()["model_id"]
        mine = client.get(f"/models/{real}").content
        invented = client.get("/models/does-not-exist-at-all").content
        assert mine == invented

    def test_a_stranger_sees_the_same_page_as_the_owner(self, client):
        """The page is the same; the API behind it is not. That is the split."""
        signup(client)
        model_id = client.post(
            "/v1/generate", json={"prompt": "a small test cube"}
        ).json()["model_id"]
        owner_view = client.get(f"/models/{model_id}").content

        client.cookies.clear()
        signup(client, "stranger@example.com", "another-long-password")
        assert client.get(f"/models/{model_id}").content == owner_view
        # ...and the data behind it is refused, unchanged.
        assert client.get(f"/v1/models/{model_id}").status_code == 404


def code_only(script: str) -> str:
    """The script with its comments removed.

    These checks are about what the code *does*, and the comments explain at
    length why it does not do exactly these things -- so scanning the raw file
    matches the prose warning against the practice. Stripping comments is what
    makes the assertion mean what it says.
    """
    out, i, n = [], 0, len(script)
    while i < n:
        two = script[i:i + 2]
        if two == "/*":
            end = script.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif two == "//":
            end = script.find("\n", i)
            i = n if end == -1 else end
        else:
            out.append(script[i])
            i += 1
    return "".join(out)


class TestNoSecretIsServed:
    SECRETS = ("local-secret", "sk_live_", "sk_test_", "whsec_", "AKIA",
               "password_hash", "session_secret", "link_secret")

    @pytest.mark.parametrize("url", URLS)
    def test_no_page_carries_a_secret(self, client, url):
        body = client.get(url).text.lower()
        for needle in self.SECRETS:
            assert needle.lower() not in body, f"{url} contains {needle}"

    def test_no_asset_carries_a_secret(self, client):
        for path in ASSETS:
            body = client.get(path).text
            for needle in self.SECRETS:
                assert needle.lower() not in body.lower(), f"{path} contains {needle}"

    def test_the_script_holds_no_bearer_token_machinery(self, client):
        """The session is an HttpOnly cookie. A token in storage would be
        readable by any script on the page, which is the position this design
        exists to avoid -- so the machinery for one must not be here."""
        script = code_only(client.get("/static/app.js").text)
        for banned in ("localStorage", "sessionStorage", "Authorization"):
            assert banned not in script, f"app.js reaches for {banned}"

    def test_the_script_never_assigns_innerhtml(self, client):
        """User prompts and model source are rendered as text. One innerHTML
        with API data would undo that for the whole front end."""
        script = code_only(client.get("/static/app.js").text)
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            assert banned not in script, f"app.js uses {banned}"

    def test_the_script_logs_nothing(self, client):
        """Prompts, tokens and model data must not reach the browser console,
        where they outlive the page and end up in screenshots."""
        assert "console." not in code_only(client.get("/static/app.js").text)


class TestUnauthenticatedVisitorsAreHandledSafely:
    """The pages are static, so 'redirect' happens client-side. What the
    server must guarantee is that the *data* is refused."""

    @pytest.mark.parametrize("path", [
        "/v1/auth/me", "/v1/account/credits", "/v1/account/history",
    ])
    def test_the_data_behind_a_private_page_is_401_without_a_session(
        self, client, path
    ):
        assert client.get(path).status_code == 401

    def test_the_page_itself_does_not_401(self, client):
        """Deliberate: a 401 on the HTML would leave a browser showing a JSON
        error instead of a sign-in form."""
        assert client.get("/dashboard").status_code == 200

    def test_the_sign_in_form_is_reachable_without_a_session(self, client):
        assert client.get("/login").status_code == 200
        assert client.get("/signup").status_code == 200


class TestTheFreeGatewayHasNoFrontEnd:
    """No accounts means no account pages -- the same rule the account routes
    already follow. A sign-in form against a gateway with no sign-in is a door
    onto a wall."""

    @pytest.fixture
    def open_app(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FORMFORGE_ALLOW_UNSAFE_SANDBOX", "1")
        return create_app(store_dir=tmp_path / "open", allow_unsafe_sandbox=True)

    def test_no_pages_are_served(self, open_app):
        with TestClient(open_app) as client:
            for url in ("/", "/login", "/dashboard", "/static/app.js"):
                assert client.get(url).status_code == 404, url

    def test_the_api_still_answers(self, open_app):
        with TestClient(open_app) as client:
            assert client.get("/v1/templates").status_code == 200
