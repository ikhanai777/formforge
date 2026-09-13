"""One recompute cycle: ingest, score, fuse, persist, alert.

This is the module a scheduler calls. Everything it does is per-asset and per-layer
fault-isolated: a failure in the news feed costs the sentiment layer and nothing else,
a failure in the price feed costs that asset and not the other one. The reason is not
robustness for its own sake -- it is that the alternative (one exception aborts the
cycle) means a single flaky free-tier endpoint can stop the product from publishing
anything, and the user cannot tell the difference between "no signal" and "no data".

Two decisions that are easy to get wrong and expensive to fix later:

**Not every recompute writes a signal row.** Spec section 5 wants every signal ever
issued tracked and graded. If a five-minute loop wrote a row per cycle, the track
record would be 288 rows a day describing the same call, and the published win rate
would be an average over recomputes rather than over signals. So a row is written when
the call *changes* materially, or when the last row is older than the heartbeat -- and
the grading code can then treat one row as one signal.

**Indicators are computed on closed bars only.** ``Series.closed()`` is applied in the
price provider, so the forming bar never reaches a score. A signal that changes because
the current hour is half-formed is a signal that changes for no market reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import alerts as alerts_module
from .config import ASSETS, LAYERS, Settings
from .fusion import Signal, fuse
from .indicators import Indicators, compute
from .providers import Feeds, build, macro_series_ids
from .providers.base import LayerData
from .providers.prices import PriceQuality
from .regime import detect, weights_for
from .scoring import macro as macro_layer
from .scoring import positioning as positioning_layer
from .scoring import sentiment as sentiment_layer
from .scoring import technical as technical_layer
from .scoring.layer import LayerScore, unavailable
from .scoring.llm_news import NewsScorer, build_scorer
from .store import Store
from .structure import levels as structure_levels

log = logging.getLogger("bullion.pipeline")

# How long a stored signal stays the current one before a row is written even though
# nothing changed. Four hours keeps the table readable while guaranteeing the track
# record has a point to grade at least that often.
HEARTBEAT = timedelta(hours=4)

# News lookback. Matches the sentiment layer's own 72h cutoff.
NEWS_WINDOW = timedelta(hours=72)


@dataclass
class AssetResult:
    """What one asset's pass produced, including what it failed to produce."""

    asset: str
    signal: Signal | None = None
    signal_id: str | None = None
    persisted: bool = False
    alerts: list[alerts_module.Alert] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.signal is not None


@dataclass
class CycleResult:
    """The whole cycle. ``synthetic`` is propagated for the UI banner."""

    started_at: datetime
    finished_at: datetime
    results: dict[str, AssetResult] = field(default_factory=dict)
    synthetic: bool = False
    feeds: dict[str, Any] = field(default_factory=dict)

    @property
    def signals(self) -> dict[str, Signal]:
        return {asset: r.signal for asset, r in self.results.items() if r.signal is not None}

    @property
    def alerts(self) -> list[alerts_module.Alert]:
        return [alert for r in self.results.values() for alert in r.alerts]

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_seconds": round((self.finished_at - self.started_at).total_seconds(), 2),
            "synthetic": self.synthetic,
            "feeds": self.feeds,
            "assets": {
                asset: {
                    "ok": r.ok,
                    "error": r.error,
                    "failures": r.failures,
                    "persisted": r.persisted,
                    "signal": r.signal.as_dict() if r.signal else None,
                    "alerts": [a.as_dict() for a in r.alerts],
                }
                for asset, r in self.results.items()
            },
        }


def collect(
    asset: str,
    feeds: Feeds,
    interval: str = "1day",
    bars: int = 400,
    now: datetime | None = None,
) -> tuple[LayerData, PriceQuality]:
    """Ingest every layer for one asset. Price failure raises; the rest are recorded.

    The asymmetry is the point: no price means no signal, while no COT report means a
    three-layer signal with lower confidence, which is still worth publishing.
    """
    now = now or datetime.now(timezone.utc)
    data = LayerData(asset=asset, synthetic=feeds.synthetic)

    series, quality = feeds.prices.validated(asset, interval, bars, now=now)
    data.series = series

    if feeds.macro is not None:
        for series_id in macro_series_ids():
            try:
                data.macro[series_id] = feeds.macro.series(series_id)  # type: ignore[attr-defined]
            except Exception as exc:
                data.note_failure(f"macro:{series_id}", exc)
    else:
        data.note_failure("macro", "no macro provider configured")

    if feeds.news is not None:
        try:
            data.headlines = list(
                feeds.news.headlines(now - NEWS_WINDOW, 60)  # type: ignore[attr-defined]
            )
        except Exception as exc:
            data.note_failure("news", exc)
    else:
        data.note_failure("news", "no news provider configured")

    if feeds.positioning is not None:
        try:
            data.cot = feeds.positioning.history(asset)  # type: ignore[attr-defined]
        except Exception as exc:
            data.note_failure("positioning", exc)
    else:
        data.note_failure("positioning", "no positioning provider configured")

    data.events = list(feeds.events)
    return data, quality


def score_layers(
    asset: str,
    data: LayerData,
    indicators: Indicators,
    scorer: NewsScorer,
    now: datetime | None = None,
) -> dict[str, LayerScore]:
    """Run all four scorers, converting any failure into an unavailable layer.

    A scorer that raises is a bug, and it is reported as one in the layer's reason
    string rather than crashing the cycle -- but it is never silently scored as neutral.
    """
    now = now or datetime.now(timezone.utc)
    assert data.series is not None  # collect() guarantees this
    out: dict[str, LayerScore] = {}

    try:
        out["technical"] = technical_layer.score(data.series, indicators)
    except Exception as exc:
        log.exception("%s: technical layer failed", asset)
        out["technical"] = unavailable("technical", f"scorer error: {exc}")

    if data.macro:
        try:
            out["macro"] = macro_layer.score(asset, data.macro, data.events)
        except Exception as exc:
            log.exception("%s: macro layer failed", asset)
            out["macro"] = unavailable("macro", f"scorer error: {exc}")
    else:
        out["macro"] = unavailable(
            "macro", data.failures.get("macro", "no macro series ingested")
        )

    if data.headlines:
        try:
            out["sentiment"] = sentiment_layer.score(asset, data.headlines, scorer, now)
        except Exception as exc:
            log.exception("%s: sentiment layer failed", asset)
            out["sentiment"] = unavailable("sentiment", f"scorer error: {exc}")
    else:
        out["sentiment"] = unavailable(
            "sentiment", data.failures.get("news", "no headlines ingested")
        )

    if data.cot is not None:
        try:
            out["positioning"] = positioning_layer.score(asset, data.cot, now.date())
        except Exception as exc:
            log.exception("%s: positioning layer failed", asset)
            out["positioning"] = unavailable("positioning", f"scorer error: {exc}")
    else:
        out["positioning"] = unavailable(
            "positioning", data.failures.get("positioning", "no COT history ingested")
        )

    return out


def evaluate(
    asset: str,
    data: LayerData,
    settings: Settings,
    scorer: NewsScorer,
    quality: PriceQuality | None = None,
    now: datetime | None = None,
) -> Signal:
    """Score, weight by regime, and fuse. No I/O -- this is the testable core.

    Separated from ``run_cycle`` so the backtester can replay a historical ``LayerData``
    through exactly the code path that produced the live call. If the backtest used a
    different scoring path, its win rate would describe a system that never ran.
    """
    now = now or datetime.now(timezone.utc)
    assert data.series is not None
    indicators = compute(data.series)
    data.indicators = indicators
    regime_read = detect(indicators, data.events, now)
    layers = score_layers(asset, data, indicators, scorer, now)

    available = {name for name, layer in layers.items() if layer.available}
    base = weights_for(settings, asset, regime_read.regime)
    weights = base.normalised(available)
    coverage = base.coverage(available)

    notes: list[str] = []
    if data.synthetic:
        notes.append(
            "SYNTHETIC DATA — generated prices, macro, positioning and news. "
            "For demonstration and testing only."
        )
    if quality is not None and quality.disagreement_bp and quality.agreement < 0.9:
        notes.append(quality.note)

    return fuse(
        asset=asset,
        layers=layers,
        weights=weights,
        indicators=indicators,
        regime=regime_read,
        thresholds=settings.thresholds,
        price_quality=quality,
        structure=structure_levels(data.series),
        coverage=coverage,
        now=now,
        synthetic=data.synthetic,
        notes=tuple(notes),
    )


def should_persist(
    previous: Any, signal: Signal, heartbeat: timedelta = HEARTBEAT
) -> tuple[bool, str]:
    """Whether this cycle's call is a new row in the track record. See module docstring."""
    if previous is None:
        return True, "first signal for this asset"
    if previous.state != signal.state.value:
        return True, f"state changed {previous.state} -> {signal.state.value}"
    if abs(previous.confidence - signal.confidence.value) >= 0.10:
        return True, (
            f"confidence moved {previous.confidence:.2f} -> {signal.confidence.value:.2f}"
        )
    if signal.timestamp - previous.ts >= heartbeat:
        return True, f"heartbeat ({heartbeat.total_seconds() / 3600:.0f}h since last row)"
    return False, "no material change since the last stored signal"


def run_cycle(
    settings: Settings,
    feeds: Feeds | None = None,
    store: Store | None = None,
    assets: tuple[str, ...] = ASSETS,
    interval: str = "1day",
    bars: int = 400,
    persist: bool = True,
    now: datetime | None = None,
) -> CycleResult:
    """Run one full recompute for every asset.

    ``persist=False`` runs the whole thing read-only, which is what the CLI's one-shot
    ``signal`` command uses -- inspecting the engine should not write to the track record.
    """
    started = now or datetime.now(timezone.utc)
    feeds = feeds or build(settings)
    if store is None and persist:
        store = Store(settings.store_path)
    scorer = build_scorer(settings.anthropic_key, settings.news_model)

    result = CycleResult(
        started_at=started,
        finished_at=started,
        synthetic=feeds.synthetic,
        feeds=feeds.describe(),
    )

    for asset in assets:
        outcome = AssetResult(asset=asset)
        try:
            data, quality = collect(asset, feeds, interval, bars, now=started)
            outcome.failures = dict(data.failures)
            signal = evaluate(asset, data, settings, scorer, quality, now=started)
            outcome.signal = signal

            previous = store.latest_signal(asset, interval) if store else None

            if store is not None:
                # Market data is written whether or not the signal is: the bars and the
                # macro series are the inputs a later backtest replays, and they cannot
                # be re-fetched from a free tier months from now.
                _persist_inputs(store, asset, data)
                persist_headlines(store, data.headlines, scorer)

            if store is not None and persist:
                write, why = should_persist(previous, signal)
                if write:
                    outcome.signal_id = store.write_signal(signal)
                    outcome.persisted = True
                    log.info("%s: stored signal (%s)", asset, why)
                else:
                    log.debug("%s: not stored (%s)", asset, why)

            if store is not None:
                _persist_indicators(store, asset, data)

            raised = alerts_module.evaluate(
                signal=signal,
                previous=previous,
                series=data.series,
                store=store if persist else None,
                thresholds=settings.thresholds,
                signal_id=outcome.signal_id,
                now=started,
            )
            outcome.alerts = raised
            if raised and persist:
                alerts_module.deliver(raised, store)

        except Exception as exc:
            log.exception("%s: cycle failed", asset)
            outcome.error = f"{type(exc).__name__}: {exc}"

        result.results[asset] = outcome

    result.finished_at = datetime.now(timezone.utc)
    return result


def _persist_inputs(store: Store, asset: str, data: LayerData) -> None:
    """Write the ingested inputs. Failures here are logged, not raised.

    Opposite posture to ``write_signal``: a lost price row can be re-fetched on the next
    cycle, and failing the whole pass because the macro cache could not be updated would
    trade a real signal for a housekeeping problem.
    """
    try:
        if data.series is not None and len(data.series):
            store.write_candles(data.series)
        for series in data.macro.values():
            store.write_macro(
                series.series_id,
                [(point.day, point.value) for point in series.points[-400:]],
                series.units,
                series.source,
            )
        if data.cot is not None:
            store.write_cot(
                [
                    (
                        report.asset,
                        report.report_date,
                        report.noncomm_long,
                        report.noncomm_short,
                        report.open_interest,
                        report.source,
                    )
                    for report in data.cot.reports
                ]
            )
    except Exception as exc:
        log.warning("%s: could not persist inputs: %s", asset, exc)


def _persist_indicators(store: Store, asset: str, data: LayerData) -> None:
    """Write the indicator snapshot for the scored bar. Failures are logged, not raised."""
    if data.indicators is None or data.series is None or not len(data.series):
        return
    try:
        store.write_indicators(
            asset=asset,
            interval=data.series.interval,
            ts=data.series.last.ts,
            payload=data.indicators.as_dict(),
        )
    except Exception as exc:
        log.warning("%s: could not persist indicators: %s", asset, exc)


def persist_headlines(store: Store, headlines: list[Any], scorer: NewsScorer) -> int:
    """Store headlines with their scores, for the timeline strip and later comparison."""
    if not headlines:
        return 0
    import json

    scores = scorer.score_batch(headlines)
    rows = []
    for headline in headlines:
        scored = scores.get(headline.id)
        rows.append(
            {
                "id": headline.id,
                "ts": headline.ts.astimezone(timezone.utc).isoformat(),
                "title": headline.title,
                "source": headline.source,
                "provider": headline.provider,
                "url": headline.url,
                "summary": headline.summary,
                "provider_sentiment": headline.provider_sentiment,
                "direction": scored.direction if scored else None,
                "relevance": scored.relevance if scored else None,
                "scorer": scored.source if scored else None,
                "rationale": scored.rationale if scored else None,
                "assets": json.dumps(list(headline.assets)),
            }
        )
    try:
        return store.write_headlines(rows)
    except Exception as exc:
        log.warning("could not persist headlines: %s", exc)
        return 0


__all__ = [
    "LAYERS",
    "AssetResult",
    "CycleResult",
    "collect",
    "evaluate",
    "persist_headlines",
    "run_cycle",
    "score_layers",
    "should_persist",
]
