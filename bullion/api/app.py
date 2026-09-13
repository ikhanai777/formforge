"""The signal API, and the dashboard it serves.

    GET  /                          -> the single-file dashboard
    GET  /health                    -> store counts, feed config, data mode
    GET  /v1/signals                -> latest signal per asset, tier-filtered
    GET  /v1/signals/{asset}        -> one asset's latest signal
    GET  /v1/signals/{asset}/history-> recent signals for that asset
    GET  /v1/scorecard              -> the live track record (spec section 5)
    GET  /v1/drift                  -> live performance vs the last walk-forward
    GET  /v1/timeline/{asset}       -> price, sentiment and event channels for the strip
    GET  /v1/backtests              -> stored walk-forward runs
    GET  /v1/tiers                  -> what each tier includes
    POST /v1/recompute              -> run a cycle now (pro/ops)

Reads come from the store, never from a provider. That is a product decision as much as
an engineering one: an endpoint that fetches on request would burn a 500-request monthly
budget on page refreshes, and two users loading the dashboard would see different prices.
The ingestion cycle writes; the API reads what was written.

``/v1/recompute`` is the exception and it is rate-limited in-process, because "refresh"
is the first button anyone presses and a free-tier key does not survive enthusiasm.

Every response carries the disclosure (spec section 11) via ``disclosure.annotate``, and
every response carries ``data_mode`` -- ``live`` or ``synthetic`` -- so a client cannot
render generated prices as real ones even by accident.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .. import scorecard as scorecard_module
from ..config import ASSETS, Settings, load_settings
from ..disclosure import DISCLOSURE, annotate
from ..providers import build as build_feeds
from ..store import Store
from . import tiers

log = logging.getLogger("bullion.api")

try:
    from fastapi import FastAPI, HTTPException, Query, Request
    from fastapi.responses import HTMLResponse, JSONResponse

    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    FASTAPI_AVAILABLE = False

WEBAPP = Path(__file__).resolve().parent.parent / "webapp" / "index.html"

# Minimum seconds between /v1/recompute calls, per process. One cycle is several
# provider requests; on a free tier, a held-down refresh key is an outage.
RECOMPUTE_COOLDOWN = 60.0


class State:
    """Process-wide handles. One store, one settings object, one feeds bundle.

    Feeds are built once because constructing them validates credentials and sets up the
    monthly budget files; rebuilding per request would re-read the budget from disk on
    every call and lose the in-process throttles.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()
        self.store = Store(self.settings.store_path)
        self._feeds: Any = None
        self._last_recompute = 0.0

    @property
    def feeds(self) -> Any:
        if self._feeds is None:
            self._feeds = build_feeds(self.settings)
        return self._feeds

    @property
    def data_mode(self) -> str:
        return "synthetic" if self.feeds.synthetic else "live"

    def may_recompute(self) -> float:
        """Seconds until the next recompute is allowed; 0 means now."""
        return max(0.0, RECOMPUTE_COOLDOWN - (time.monotonic() - self._last_recompute))

    def mark_recompute(self) -> None:
        self._last_recompute = time.monotonic()


def create_app(settings: Settings | None = None) -> Any:
    """Build the FastAPI app. Raises if FastAPI is not installed.

    Same pattern as formforge's API: the optional dependency is checked where it is
    needed, so importing the engine never requires a web framework.
    """
    if not FASTAPI_AVAILABLE:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "FastAPI is not installed. Install the extra: pip install 'bullion[api]'"
        )

    state = State(settings)
    app = FastAPI(
        title="Bullion signal API",
        version="0.1.0",
        description=(
            "Confidence-scored, explainable signals for gold and silver. "
            "Informational only, not financial advice."
        ),
    )
    app.state.bullion = state

    def tier_of(request: Request) -> Any:
        """Tier from a header. Real deployments resolve it from an authenticated user.

        Deliberately not pretending to be auth: ``X-Bullion-Tier`` is a development
        affordance and the docstring says so, rather than shipping a fake entitlement
        check that looks load-bearing.
        """
        return tiers.resolve(request.headers.get("X-Bullion-Tier"))

    def envelope(payload: dict[str, Any]) -> dict[str, Any]:
        out = annotate(payload)
        out["data_mode"] = state.data_mode
        return out

    # -- dashboard ---------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> Any:
        if not WEBAPP.exists():  # pragma: no cover - packaging error
            raise HTTPException(status_code=500, detail=f"dashboard missing at {WEBAPP}")
        return HTMLResponse(WEBAPP.read_text(encoding="utf-8"))

    @app.get("/health")
    def health() -> Any:
        return envelope(
            {
                "ok": True,
                "store": str(state.settings.store_path),
                "counts": state.store.counts(),
                "feeds": state.feeds.describe(),
                "live_prices": state.settings.has_live_prices,
                "news_scorer": "claude" if state.settings.anthropic_key else "lexicon",
            }
        )

    # -- signals -----------------------------------------------------------

    @app.get("/v1/signals")
    def latest_signals(request: Request) -> Any:
        tier = tier_of(request)
        out: dict[str, Any] = {}
        for asset in tiers.visible_assets(tier):
            rows = state.store.signals(asset=asset, limit=40)
            row = tiers.pick_signal(rows, tier)
            out[asset] = tiers.apply(row.payload, tier) if row else None
        return envelope(
            {
                "signals": out,
                "tier": tiers.describe(tier),
                "withheld_assets": [a for a in ASSETS if a not in set(tier.assets)],
            }
        )

    # The history route is declared BEFORE the single-asset route on purpose. Asset names
    # contain a slash ("XAU/USD"), so the path converter is greedy: with the single-asset
    # route first, a request for /v1/signals/XAU/USD/history matches it with the asset read
    # as "XAU/USD/history" and comes back 403. Route order is load-bearing here.
    @app.get("/v1/signals/{asset:path}/history")
    def signal_history(
        asset: str,
        request: Request,
        days: int = Query(30, ge=1, le=365),
        limit: int = Query(100, ge=1, le=500),
    ) -> Any:
        tier = tier_of(request)
        if asset not in set(tier.assets):
            raise HTTPException(
                status_code=403, detail=f"{asset} is not in the {tier.name} tier"
            )
        since = datetime.now(timezone.utc) - timedelta(days=days)
        rows = state.store.signals(asset=asset, since=since, limit=limit)
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=tier.delay_minutes)
        return envelope(
            {
                "asset": asset,
                "days": days,
                "signals": [
                    tiers.apply(row.payload, tier) for row in rows if row.ts <= cutoff
                ],
            }
        )

    @app.get("/v1/signals/{asset:path}")
    def latest_signal(asset: str, request: Request) -> Any:
        tier = tier_of(request)
        if asset not in set(tier.assets):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"{asset} is not included in the {tier.name} tier "
                    f"(included: {', '.join(tier.assets)})"
                ),
            )
        rows = state.store.signals(asset=asset, limit=40)
        row = tiers.pick_signal(rows, tier)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"no signal available for {asset}"
                    + (
                        f" older than the {tier.delay_minutes}-minute {tier.name}-tier delay"
                        if tier.delay_minutes
                        else ""
                    )
                ),
            )
        return envelope(tiers.apply(row.payload, tier))

    # -- track record ------------------------------------------------------

    @app.get("/v1/scorecard")
    def scorecard(
        include_synthetic: bool | None = Query(None),
        days: int | None = Query(None, ge=1, le=3650),
    ) -> Any:
        since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
        # In synthetic mode the synthetic rows are the only rows there are, and the whole
        # page is already banner-marked as generated -- so showing them is informative
        # rather than misleading. In live mode they stay excluded unless explicitly asked
        # for, because pooling generated outcomes into a published win rate is fiction.
        if include_synthetic is None:
            include_synthetic = state.data_mode == "synthetic"
        card = scorecard_module.build(
            state.store, include_synthetic=include_synthetic, since=since
        )
        return envelope(
            {
                "scorecard": card.as_dict(),
                "activity": scorecard_module.recent_activity(state.store),
                "includes_synthetic": include_synthetic,
            }
        )

    @app.get("/v1/drift")
    def drift(asset: str | None = Query(None)) -> Any:
        verdict = scorecard_module.drift_from_store(state.store, asset)
        return envelope({"drift": verdict.as_dict()})

    @app.get("/v1/backtests")
    def backtests(asset: str | None = Query(None), limit: int = Query(10, ge=1, le=50)) -> Any:
        return envelope({"runs": state.store.backtest_runs(asset, limit)})

    # -- timeline ----------------------------------------------------------

    @app.get("/v1/timeline/{asset:path}")
    def timeline(
        asset: str,
        request: Request,
        bars: int = Query(180, ge=30, le=1000),
    ) -> Any:
        """Multi-channel data for the 'tide' strip: price, sentiment, signals, events.

        One endpoint rather than three, because the strip's whole point is that the
        channels are aligned in time -- assembling them from separate requests means the
        client stitches timestamps, and the first rendering bug is an off-by-one day.
        """
        tier = tier_of(request)
        if asset not in set(tier.assets):
            raise HTTPException(
                status_code=403, detail=f"{asset} is not in the {tier.name} tier"
            )
        series = state.store.load_candles(asset, "1day", bars)
        since = datetime.now(timezone.utc) - timedelta(days=min(bars, 60))
        headlines = state.store.recent_headlines(since, 200)
        rows = state.store.signals(
            asset=asset, since=series[0].ts if len(series) else None, limit=300
        )
        return envelope(
            {
                "asset": asset,
                "price": [
                    {"ts": c.ts.isoformat(), "close": c.close, "high": c.high, "low": c.low}
                    for c in series
                ],
                "sentiment": [
                    {
                        "ts": row["ts"],
                        "direction": row["direction"],
                        "relevance": row["relevance"],
                        "title": row["title"],
                        "source": row["source"],
                    }
                    for row in headlines
                    if row.get("direction") is not None
                ],
                "signals": [
                    {
                        "ts": row.ts.isoformat(),
                        "state": row.state,
                        "confidence": row.confidence,
                        "price": row.price_at_signal,
                    }
                    for row in rows
                ],
                "events": [
                    {"ts": event.ts.isoformat(), "name": event.name, "kind": event.kind}
                    for event in state.feeds.events
                ],
            }
        )

    # -- meta --------------------------------------------------------------

    @app.get("/v1/tiers")
    def tier_list() -> Any:
        from ..config import TIERS

        return envelope({"tiers": {name: tiers.describe(t) for name, t in TIERS.items()}})

    @app.get("/v1/disclosure")
    def disclosure() -> Any:
        return envelope({"full": DISCLOSURE})

    @app.post("/v1/recompute")
    def recompute(request: Request) -> Any:
        """Run one ingestion cycle now. Throttled; see RECOMPUTE_COOLDOWN."""
        tier = tier_of(request)
        if not tier.api_access and os.environ.get("BULLION_OPEN_RECOMPUTE") != "1":
            raise HTTPException(
                status_code=403,
                detail=(
                    "recompute is a pro-tier endpoint; set BULLION_OPEN_RECOMPUTE=1 for "
                    "local development"
                ),
            )
        wait = state.may_recompute()
        if wait > 0:
            raise HTTPException(
                status_code=429,
                detail=f"recompute is throttled; try again in {wait:.0f}s",
            )
        from ..pipeline import run_cycle

        state.mark_recompute()
        result = run_cycle(state.settings, feeds=state.feeds, store=state.store)
        return JSONResponse(envelope({"cycle": result.as_dict()}))

    return app


def main() -> None:  # pragma: no cover - entry point
    """``bullion-api`` console script: uvicorn on the app."""
    import uvicorn

    uvicorn.run(
        create_app(),
        host=os.environ.get("BULLION_HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        log_level=os.environ.get("BULLION_LOG", "info"),
    )
