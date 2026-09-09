"""The browser front end, served from the same origin as the API.

Same origin is the whole design, and it is a security decision rather than a
convenience one. The session is an HttpOnly cookie: JavaScript cannot read it,
which is what makes an XSS in this front end unable to walk off with somebody's
session. A front end on another origin could not send that cookie at all
without CORS credentials and `SameSite=None`, so the alternative to this is
a bearer token in `localStorage` -- readable by any script on the page, and a
strictly worse position. There is no token here, and no API key: the browser
holds nothing but the cookie the server set.

**These routes serve markup and nothing else.** Every page is the same bytes
for every visitor, signed in or not, and every piece of data on screen arrives
from a `fetch` against the existing JSON API, which does the authorising. That
is why `/models/{model_id}` is declared in `policies.py` with `resource=None`:
it does not resolve the identifier, it ignores it. `tests/test_web.py` holds
that claim to account by asking for two different ids and comparing the
responses byte for byte.

The consequence worth stating plainly: nothing here can grant access the API
would refuse, because nothing here decides anything. Hiding a button is a
courtesy to the user, never a control.
"""

from __future__ import annotations

from pathlib import Path

WEB_ROOT = Path(__file__).parent / "web"

# The studio is the one page that does not live beside the others. It is
# `web/studio.html` at the repository root, where a test asserts its schemas
# match the generators' and five documents point at it by that path, and it is
# opened directly from disk as often as it is served. Moving it into the
# package to save this lookup would break all of that to tidy one line.
#
# The cost is stated rather than hidden: an installed (non-editable) copy has
# no repository root, so `/studio` answers 404 with that as the reason. Every
# other page is packaged normally.
STUDIO = Path(__file__).resolve().parents[2] / "web" / "studio.html"

# path -> file. One shell per page rather than a client-side router: each page
# is real semantic markup that works with the back button and reads correctly
# in view-source, and a request for a page that does not exist still 404s.
PAGES: dict[str, str] = {
    "/": "home.html",
    "/signup": "signup.html",
    "/login": "login.html",
    "/dashboard": "dashboard.html",
    "/create": "create.html",
    "/generators": "generators.html",
    "/models/{model_id}": "model.html",
    "/forgot-password": "forgot.html",
    "/reset-password": "reset.html",
    "/account": "account.html",
}

ASSETS: dict[str, tuple[str, str]] = {
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


def build_web_router():
    """The pages and their two assets, as a FastAPI router."""
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import FileResponse, Response

    router = APIRouter(include_in_schema=False)

    def _serve(name: str, media_type: str) -> Response:
        target = WEB_ROOT / name
        if not target.exists():  # pragma: no cover - packaging failure
            raise HTTPException(status_code=500, detail="front end is not installed")
        return FileResponse(
            target,
            media_type=media_type,
            # No caching while the shell is this small: a stale page against a
            # changed API is a confusing bug report, and the files are a few
            # kilobytes.
            headers={"cache-control": "no-cache"},
        )

    def _page(name: str):
        async def page() -> Response:
            return _serve(name, "text/html; charset=utf-8")

        return page

    for path, filename in PAGES.items():
        router.add_api_route(path, _page(filename), methods=["GET"])

    @router.get("/studio")
    async def studio() -> Response:
        """The parametric studio: live viewer, real sliders, real export.

        Served from the same origin as everything else, so its "Build for
        real" button carries the session cookie and the parameters on the
        sliders go to `POST /v1/templates/{id}/build`.
        """
        if not STUDIO.exists():  # pragma: no cover - only on a packaged copy
            raise HTTPException(
                status_code=404,
                detail=(
                    "the studio is served from web/studio.html in the "
                    "repository, which this installation does not have"
                ),
            )
        return FileResponse(
            STUDIO,
            media_type="text/html; charset=utf-8",
            headers={"cache-control": "no-cache"},
        )

    def _asset(name: str, media_type: str):
        async def asset() -> Response:
            return _serve(name, media_type)

        return asset

    for path, (filename, media_type) in ASSETS.items():
        router.add_api_route(path, _asset(filename, media_type), methods=["GET"])

    return router
