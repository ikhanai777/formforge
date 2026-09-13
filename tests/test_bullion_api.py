"""The HTTP surface, the tier gating, and the two claims the product must never break.

The disclosure test at the bottom is the one worth keeping honest: spec section 11 says the
vocabulary matters as much as the disclaimer, so the suite greps the user-facing strings for
the phrasings the product is not allowed to use about itself. It is the cheapest possible
guard against the copy drifting back toward "highly accurate" as the thing gets polished.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from bullion_fixtures import make_signal

from bullion.api import tiers
from bullion.config import ASSETS, GOLD, SILVER, TIERS, Settings
from bullion.disclosure import DISCLOSURE, FORBIDDEN_PHRASINGS, SHORT_DISCLOSURE, annotate
from bullion.fusion import SignalState
from bullion.store import Store

UTC = timezone.utc

fastapi = pytest.importorskip("fastapi", reason="the API extra is not installed")
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent


def _negated(text: str, phrase: str) -> bool:
    """"not financial advice" and "no guarantee" are the point, not a violation."""
    return f"not {phrase}" in text or f"no {phrase}" in text or f"never {phrase}" in text


def _python_offenders(path: Path, phrase: str) -> list[str]:
    """String literals in a module, excluding docstrings."""
    import ast

    tree = ast.parse(path.read_text())
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))

    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings:
            continue
        text = node.value.lower()
        if phrase in text and not _negated(text, phrase):
            found.append(f"{path.relative_to(REPO)}:{node.lineno}: {node.value[:90]}")
    return found


def _html_offenders(path: Path, phrase: str) -> list[str]:
    """Visible page text, with comments and the stylesheet removed."""
    import re

    text = path.read_text()
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"(?m)^\s*//.*$", " ", text)
    lowered = text.lower()
    if phrase in lowered and not _negated(lowered, phrase):
        return [f"{path.relative_to(REPO)}: contains {phrase!r}"]
    return []


@pytest.fixture()
def store_path(tmp_path) -> Path:
    return tmp_path / "api.db"


@pytest.fixture()
def seeded(store_path) -> Store:
    """A store with one current signal per asset plus one four-hour-old gold signal."""
    store = Store(store_path)
    now = datetime.now(UTC)
    store.write_signal(
        make_signal(asset=GOLD, state=SignalState.BUY, ts=now - timedelta(hours=6))
    )
    store.write_signal(
        make_signal(asset=GOLD, state=SignalState.STRONG_BUY, confidence=0.8, ts=now)
    )
    store.write_signal(make_signal(asset=SILVER, state=SignalState.SELL, ts=now))
    return store


@pytest.fixture()
def client(store_path, seeded) -> TestClient:
    from bullion.api.app import create_app

    return TestClient(create_app(Settings(store_path=store_path)))


class TestTierLogic:
    def test_unknown_tiers_do_not_get_a_pass(self):
        assert tiers.resolve("enterprise").name == "free"
        assert tiers.resolve(None).name == "free"
        assert tiers.resolve("PRO").name == "pro"

    def test_the_free_tier_sees_gold_only(self):
        assert tiers.visible_assets(TIERS["free"]) == [GOLD]
        assert set(tiers.visible_assets(TIERS["paid"])) == set(ASSETS)

    def test_the_delay_serves_a_real_older_signal_not_a_faked_timestamp(self):
        from bullion.store import SignalRow

        now = datetime.now(UTC)
        rows = [
            SignalRow(
                id=str(age), asset=GOLD, interval="1day", ts=now - timedelta(hours=age),
                state="BUY", strength="normal", composite_score=0.4, confidence=0.6,
                price_at_signal=2400.0, suggested_stop=2380.0, suggested_target=2450.0,
                regime="trend", synthetic=False, payload={},
            )
            for age in (0, 2, 6)
        ]
        picked = tiers.pick_signal(rows, TIERS["free"], now=now)
        # Four-hour delay: the six-hour-old row is the newest one it may serve.
        assert picked.id == "6"
        assert tiers.pick_signal(rows, TIERS["paid"], now=now).id == "0"

    def test_no_eligible_signal_returns_none_rather_than_the_newest(self):
        from bullion.store import SignalRow

        now = datetime.now(UTC)
        fresh = [
            SignalRow(
                id="new", asset=GOLD, interval="1day", ts=now, state="BUY", strength="normal",
                composite_score=0.4, confidence=0.6, price_at_signal=2400.0,
                suggested_stop=None, suggested_target=None, regime="trend", synthetic=False,
                payload={},
            )
        ]
        assert tiers.pick_signal(fresh, TIERS["free"], now=now) is None

    def test_gating_explains_itself_rather_than_silently_omitting(self):
        payload = make_signal().as_dict()
        gated = tiers.apply(payload, TIERS["free"])
        assert "locked" in gated["reasoning"]
        assert "locked" in gated["levels"]
        assert gated["suggested_stop"] is None
        # Confidence survives: it is the headline of the product, and a bare word with no
        # way to judge it would be a worse free tier than a delayed one.
        assert gated["confidence"] == payload["confidence"]
        assert gated["tier"] == "free"

    def test_paid_tier_keeps_everything(self):
        payload = make_signal().as_dict()
        full = tiers.apply(payload, TIERS["paid"])
        assert full["reasoning"] == payload["reasoning"]
        assert full["suggested_stop"] == payload["suggested_stop"]


class TestEndpoints:
    def test_health_reports_the_data_mode_and_counts(self, client):
        body = client.get("/health").json()
        assert body["ok"] is True
        assert body["data_mode"] in ("live", "synthetic")
        assert body["counts"]["signals"] == 3
        assert body["disclosure"] == SHORT_DISCLOSURE

    def test_latest_signals_are_tier_filtered(self, client):
        paid = client.get("/v1/signals", headers={"X-Bullion-Tier": "paid"}).json()
        assert set(paid["signals"]) == set(ASSETS)
        assert paid["withheld_assets"] == []

        free = client.get("/v1/signals", headers={"X-Bullion-Tier": "free"}).json()
        assert set(free["signals"]) == {GOLD}
        assert free["withheld_assets"] == [SILVER]
        # And the free tier's gold signal is the delayed one.
        assert free["signals"][GOLD]["signal"] == "BUY"

    def test_a_withheld_asset_is_a_403_that_names_the_tier(self, client):
        response = client.get(f"/v1/signals/{SILVER}", headers={"X-Bullion-Tier": "free"})
        assert response.status_code == 403
        assert "free tier" in response.json()["detail"]

    def test_missing_signals_return_404_explaining_the_delay(self, store_path):
        from bullion.api.app import create_app

        store = Store(store_path)
        store.write_signal(make_signal(asset=GOLD, ts=datetime.now(UTC)))
        client = TestClient(create_app(Settings(store_path=store_path)))
        response = client.get(f"/v1/signals/{GOLD}", headers={"X-Bullion-Tier": "free"})
        assert response.status_code == 404
        assert "delay" in response.json()["detail"]

    def test_history_respects_the_tier_delay(self, client):
        body = client.get(
            f"/v1/signals/{GOLD}/history", headers={"X-Bullion-Tier": "free"}
        ).json()
        assert all(
            datetime.fromisoformat(row["timestamp"]) <= datetime.now(UTC) - timedelta(hours=4)
            for row in body["signals"]
        )

    def test_scorecard_and_drift_are_served(self, client):
        card = client.get("/v1/scorecard").json()
        assert "overall" in card["scorecard"]
        assert str(card["scorecard"]["caveat"])
        drift = client.get("/v1/drift").json()
        assert drift["drift"]["verdict"] in (
            "no_baseline", "insufficient_sample", "in_line", "underperforming", "outperforming"
        )

    def test_timeline_returns_aligned_channels(self, client):
        body = client.get(f"/v1/timeline/{GOLD}").json()
        assert set(body) >= {"price", "sentiment", "signals", "events"}
        assert isinstance(body["signals"], list)

    def test_tiers_and_disclosure_endpoints(self, client):
        body = client.get("/v1/tiers").json()
        assert set(body["tiers"]) == {"free", "paid", "pro"}
        assert body["tiers"]["free"]["delay_minutes"] == 240
        assert client.get("/v1/disclosure").json()["full"] == DISCLOSURE

    def test_recompute_is_gated_and_throttled(self, client, monkeypatch):
        assert client.post("/v1/recompute").status_code == 403
        monkeypatch.setenv("BULLION_OPEN_RECOMPUTE", "1")
        first = client.post("/v1/recompute")
        assert first.status_code == 200
        assert "cycle" in first.json()
        # The first button anyone presses is refresh; a free-tier key does not survive
        # enthusiasm, so the second call inside the window is a 429.
        second = client.post("/v1/recompute")
        assert second.status_code == 429
        assert "throttled" in second.json()["detail"]

    def test_the_dashboard_is_served_and_self_contained(self, client):
        import re

        body = client.get("/").text
        assert body.startswith("<!doctype html>")
        assert "<script" in body
        # No build step and no third-party fetches: a dashboard that pulls React from a
        # CDN renders blank on a locked-down network or an offline demo.
        external = re.findall(r'(?:src|href)\s*=\s*"(https?://[^"]+)"', body)
        assert external == []

    def test_every_response_carries_the_disclosure(self, client):
        for url in ("/health", "/v1/signals", "/v1/scorecard", "/v1/tiers", "/v1/drift"):
            assert client.get(url).json()["disclosure"] == SHORT_DISCLOSURE
        assert client.get(f"/v1/signals/{GOLD}").json()["disclosure"]

    def test_every_response_declares_the_data_mode(self, client):
        for url in ("/health", "/v1/signals", "/v1/scorecard"):
            assert client.get(url).json()["data_mode"] in ("live", "synthetic")


class TestDisclosureDiscipline:
    def test_annotate_attaches_the_short_form(self):
        assert annotate({"a": 1})["disclosure"] == SHORT_DISCLOSURE

    def test_the_long_form_says_the_important_things(self):
        lowered = DISCLOSURE.lower()
        for required in (
            "not investment advice",
            "probabilistic",
            "past performance",
            "not a registered investment adviser",
        ):
            assert required in lowered

    @pytest.mark.parametrize("phrase", sorted(FORBIDDEN_PHRASINGS))
    def test_the_product_never_says_it_about_itself(self, phrase):
        """Scan every user-facing string in the package for banned phrasing.

        Spec section 11: an unlicensed product that implies guaranteed profitability is a
        regulatory problem, and the vocabulary is what a regulator reads first.

        Strings are collected with ``ast`` rather than by grepping lines, so that
        docstrings and comments are exempt -- they exist to *explain* these rules and
        necessarily quote the phrasings -- while anything that can reach a user is not. For
        HTML the comments and the stylesheet are stripped and the rest is scanned.
        """
        offenders: list[str] = []
        for path in sorted((REPO / "bullion").rglob("*")):
            if path.name == "disclosure.py":
                continue
            if path.suffix == ".py":
                offenders.extend(_python_offenders(path, phrase))
            elif path.suffix == ".html":
                offenders.extend(_html_offenders(path, phrase))
        assert not offenders, (
            f"banned phrasing {phrase!r} ({FORBIDDEN_PHRASINGS[phrase]}):\n"
            + "\n".join(offenders)
        )

    def test_signal_headlines_are_descriptive_not_imperative(self):
        for state in SignalState:
            signal = make_signal(
                state=state,
                stop=2380.0 if state.direction else None,
                target=2450.0 if state.direction else None,
            )
            text = signal.headline().lower()
            for banned in ("you should", "we recommend", "buy now", "sell now", "act now"):
                assert banned not in text
