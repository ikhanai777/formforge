"""Walk-forward backtesting and weight fitting.

Spec section 5, and the sentence that dictates the architecture: "A model tuned and
tested on the same window will look 'highly accurate' and fail live." Everything here
is arranged so that the number this module publishes is an out-of-sample number.

Three design decisions carry that:

**Layer scores are computed once, per bar, before any weighting.** A layer score does
not depend on the weights, so the expensive part (indicators, structure, macro slicing,
COT percentiles) runs once per bar and every candidate weight vector is then a cheap
re-fusion of cached frames. That is what makes fitting affordable in pure Python -- and
it also guarantees the fitted weights were evaluated against exactly the layer scores
the live engine would have produced, rather than a re-derivation that might differ.

**History is sliced by as-of date, never by index.** ``Series.slice_until``,
``MacroSeries`` filtered by day, ``CotHistory`` filtered by report date, headlines
filtered by timestamp. Every lookahead bug is an off-by-one in a slice, so there is
exactly one way to take a slice and it takes a timestamp.

**Ties inside a bar go to the stop.** Without tick data it is unknowable whether the
high or the low came first, and resolving ties favourably is the single most common way
a backtest flatters itself. A trade whose bar touched both levels is graded a loss.

What this module will not do is pretend that fitting on synthetic data means anything:
``fit_weights`` warns, loudly, when the series it was handed came from the generator.
"""

from __future__ import annotations

import itertools
import logging
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import pipeline
from .config import LAYERS, LayerWeights, Settings
from .fusion import Signal, SignalState, fuse
from .indicators import Indicators, compute
from .providers.base import CotHistory, Headline, LayerData, MacroSeries
from .regime import RegimeRead, detect
from .scoring.layer import LayerScore
from .scoring.llm_news import LexiconScorer, NewsScorer
from .series import Series, stdev
from .structure import Level
from .structure import levels as structure_levels

log = logging.getLogger("bullion.backtest")

# Bars to give a signal before grading it a timeout. Seven is the spec's own horizon
# (section 5 resolves outcomes at +1d/+3d/+7d) and matches the swing timeframe the
# daily signals are built for.
DEFAULT_HORIZON = 7

# Bars of warmup before the first signal. 250 covers the 200-EMA plus enough history
# for the bandwidth percentile to mean anything.
WARMUP = 250


@dataclass
class History:
    """All four layers' history for one asset, sliceable by as-of date.

    Holding the whole history and slicing per bar is deliberately the slow, safe way
    round. The fast way -- incrementally updating state as the replay walks forward -- is
    how lookahead gets in, because the state carries something from the future that
    nobody notices for six months.
    """

    asset: str
    series: Series
    macro: dict[str, MacroSeries] = field(default_factory=dict)
    cot: CotHistory | None = None
    headlines: Sequence[Headline] = ()
    synthetic: bool = False

    def as_of(self, when: datetime) -> LayerData:
        """The data that was available at ``when``, and nothing that was not."""
        day = when.date()
        macro = {
            key: MacroSeries(
                series_id=series.series_id,
                label=series.label,
                points=tuple(p for p in series.points if p.day <= day),
                units=series.units,
                source=series.source,
            )
            for key, series in self.macro.items()
        }
        macro = {key: series for key, series in macro.items() if series.points}

        cot = None
        if self.cot is not None:
            reports = tuple(r for r in self.cot.reports if r.report_date <= day)
            if reports:
                cot = CotHistory(asset=self.asset, reports=reports)

        return LayerData(
            asset=self.asset,
            series=self.series.slice_until(when),
            macro=macro,
            headlines=[h for h in self.headlines if h.ts <= when],
            cot=cot,
            events=[],
            synthetic=self.synthetic,
        )


@dataclass(frozen=True)
class Frame:
    """One bar's worth of scoring work, cached so re-weighting is cheap."""

    ts: datetime
    indicators: Indicators
    regime: RegimeRead
    layers: dict[str, LayerScore]
    structure: list[Level]
    coverage_layers: frozenset[str]


@dataclass(frozen=True)
class Trade:
    """A graded signal. ``r_multiple`` is the unit everything is reported in.

    R (multiples of the risk taken to the suggested stop) rather than percent, because
    it is the only unit in which a gold trade and a silver trade are comparable -- silver's
    stops are wider by construction, so a percent return flatters silver and a point
    return flatters gold.
    """

    asset: str
    ts: datetime
    state: str
    strength: str
    direction: int
    confidence: float
    composite: float
    regime: str
    entry: float
    stop: float
    target: float
    outcome: str  # "target" | "stop" | "timeout"
    exit_price: float
    bars_held: int
    r_multiple: float
    mfe_r: float
    mae_r: float
    price_1d: float | None = None
    price_3d: float | None = None
    price_7d: float | None = None

    @property
    def win(self) -> bool:
        return self.r_multiple > 0

    def signed_return(self, price: float | None) -> float | None:
        if price is None or not self.entry:
            return None
        return self.direction * (price - self.entry) / self.entry

    def as_dict(self) -> dict[str, object]:
        return {
            "asset": self.asset,
            "ts": self.ts.isoformat(),
            "state": self.state,
            "strength": self.strength,
            "confidence": round(self.confidence, 4),
            "composite": round(self.composite, 4),
            "regime": self.regime,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "outcome": self.outcome,
            "exit_price": self.exit_price,
            "bars_held": self.bars_held,
            "r_multiple": round(self.r_multiple, 4),
            "mfe_r": round(self.mfe_r, 4),
            "mae_r": round(self.mae_r, 4),
            "return_1d": self.signed_return(self.price_1d),
            "return_3d": self.signed_return(self.price_3d),
            "return_7d": self.signed_return(self.price_7d),
        }


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


def resolve(
    signal: Signal,
    future: Series,
    horizon: int = DEFAULT_HORIZON,
) -> Trade | None:
    """Grade one signal against the bars that followed it.

    Returns None for a signal with no levels (HOLD, NO_SIGNAL) or with no future bars --
    an ungraded signal is excluded from the win rate rather than counted as a miss,
    because counting unresolved trades as losses understates a live system at exactly
    the moment its newest calls are still open.
    """
    if signal.direction == 0 or signal.levels.stop is None or signal.levels.target is None:
        return None
    bars = future.candles[:horizon]
    if not bars:
        return None

    entry = signal.price_at_signal
    stop = signal.levels.stop
    target = signal.levels.target
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    long_ = signal.direction > 0

    outcome = "timeout"
    exit_price = bars[-1].close
    bars_held = len(bars)
    mfe = 0.0
    mae = 0.0

    for index, candle in enumerate(bars, start=1):
        favourable = (candle.high - entry) if long_ else (entry - candle.low)
        adverse = (entry - candle.low) if long_ else (candle.high - entry)
        mfe = max(mfe, favourable / risk)
        mae = max(mae, adverse / risk)

        hit_stop = candle.low <= stop if long_ else candle.high >= stop
        hit_target = candle.high >= target if long_ else candle.low <= target
        # Stop first on a tie: see the module docstring.
        if hit_stop:
            outcome, exit_price, bars_held = "stop", stop, index
            break
        if hit_target:
            outcome, exit_price, bars_held = "target", target, index
            break

    direction = 1 if long_ else -1
    r_multiple = direction * (exit_price - entry) / risk

    def close_at(offset: int) -> float | None:
        return bars[offset - 1].close if len(bars) >= offset else None

    return Trade(
        asset=signal.asset,
        ts=signal.timestamp,
        state=signal.state.value,
        strength=signal.state.strength,
        direction=direction,
        confidence=signal.confidence.value,
        composite=signal.composite_score,
        regime=signal.regime.regime.value,
        entry=entry,
        stop=stop,
        target=target,
        outcome=outcome,
        exit_price=exit_price,
        bars_held=bars_held,
        r_multiple=r_multiple,
        mfe_r=mfe,
        mae_r=mae,
        price_1d=close_at(1),
        price_3d=close_at(3),
        price_7d=close_at(7),
    )


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def build_frames(
    history: History,
    settings: Settings,
    scorer: NewsScorer | None = None,
    warmup: int = WARMUP,
    step: int = 1,
    limit: int | None = None,
) -> list[Frame]:
    """Score every layer at every bar, once. The expensive half of a backtest.

    ``step`` thins the replay (every nth bar) for a quick pass; the default of 1 scores
    every bar, which is what a published win rate should be computed on.
    """
    scorer = scorer or LexiconScorer()
    frames: list[Frame] = []
    bars = history.series.candles
    end = len(bars) if limit is None else min(len(bars), warmup + limit * step)

    for index in range(warmup, end, step):
        when = bars[index].ts
        data = history.as_of(when)
        if data.series is None or len(data.series) < 30:
            continue
        try:
            indicators = compute(data.series)
        except ValueError:
            continue
        regime = detect(indicators, data.events, when)
        layers = pipeline.score_layers(history.asset, data, indicators, scorer, when)
        frames.append(
            Frame(
                ts=when,
                indicators=indicators,
                regime=regime,
                layers=layers,
                structure=structure_levels(data.series),
                coverage_layers=frozenset(
                    name for name, layer in layers.items() if layer.available
                ),
            )
        )
    return frames


def grade_frames(
    frames: Sequence[Frame],
    history: History,
    settings: Settings,
    weights: LayerWeights,
    horizon: int = DEFAULT_HORIZON,
    apply_regime_tilt: bool = True,
) -> list[Trade]:
    """Fuse and grade a run of frames under one weight set.

    ``apply_regime_tilt`` mirrors the live engine, which tilts the base weights by the
    detected regime before normalising. It is switchable because fitting *base* weights
    through the tilt is the honest thing to do (it is what will run) while fitting
    without it isolates the weights from the tilt table when you want to know which of
    the two is carrying the result.
    """
    trades: list[Trade] = []
    for frame in frames:
        base = (
            weights.tilted(_tilt_for(frame.regime)) if apply_regime_tilt else weights
        )
        available = set(frame.coverage_layers)
        signal = fuse(
            asset=history.asset,
            layers=frame.layers,
            weights=base.normalised(available),
            indicators=frame.indicators,
            regime=frame.regime,
            thresholds=settings.thresholds,
            structure=frame.structure,
            coverage=base.coverage(available),
            now=frame.ts,
        )
        if signal.state in (SignalState.HOLD, SignalState.NO_SIGNAL):
            continue
        future = history.series.after(frame.ts)
        trade = resolve(signal, future, horizon)
        if trade is not None:
            trades.append(trade)
    return trades


def _tilt_for(regime: RegimeRead) -> dict[str, float]:
    from .config import REGIME_TILT

    return REGIME_TILT[regime.regime]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% confidence interval on a win rate, Wilson score.

    Published next to every win rate in the app, because "62% win rate" from 13 trades
    and from 400 trades are different claims and a bare percentage hides which one you
    are looking at. Wilson rather than normal approximation: it does not fall apart at
    small n or near 0 and 1, which is exactly where a new signal product lives.
    """
    if n <= 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def metrics(trades: Sequence[Trade]) -> dict[str, object]:
    """Summary statistics for a set of graded trades.

    ``expectancy`` is the headline number: average R per signal. Win rate alone is
    gameable by moving the target closer, which is why it is never reported here
    without expectancy beside it.
    """
    n = len(trades)
    if n == 0:
        return {
            "trades": 0,
            "win_rate": None,
            "win_rate_ci": None,
            "expectancy_r": None,
            "profit_factor": None,
            "avg_win_r": None,
            "avg_loss_r": None,
            "max_drawdown_r": None,
            "sharpe": None,
            "target_hit_rate": None,
            "stop_hit_rate": None,
            "avg_bars_held": None,
        }
    rs = [t.r_multiple for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)

    spread = stdev(rs)
    return {
        "trades": n,
        "win_rate": len(wins) / n,
        "win_rate_ci": list(wilson_interval(len(wins), n)),
        "expectancy_r": sum(rs) / n,
        "profit_factor": (gross_win / gross_loss) if gross_loss else None,
        "avg_win_r": (gross_win / len(wins)) if wins else None,
        "avg_loss_r": (sum(losses) / len(losses)) if losses else None,
        "max_drawdown_r": drawdown,
        # Per-trade Sharpe-alike: mean R over the spread of R. Not annualised, because
        # annualising a signal count is a made-up number.
        "sharpe": (sum(rs) / n / spread) if spread else None,
        "target_hit_rate": sum(1 for t in trades if t.outcome == "target") / n,
        "stop_hit_rate": sum(1 for t in trades if t.outcome == "stop") / n,
        "avg_bars_held": sum(t.bars_held for t in trades) / n,
    }


def objective(trades: Sequence[Trade], shrink: float = 10.0) -> float:
    """What ``fit_weights`` maximises: expectancy, shrunk by sample size.

    Raw expectancy picks the weight set that found four lucky trades. Shrinking by
    ``n/(n+10)`` prefers a slightly worse edge measured over forty trades, which is the
    preference a walk-forward test would enforce anyway -- this just stops the optimiser
    wasting folds discovering it.
    """
    if not trades:
        return -1.0
    n = len(trades)
    expectancy = sum(t.r_multiple for t in trades) / n
    return expectancy * (n / (n + shrink))


# ---------------------------------------------------------------------------
# Fitting and walk-forward
# ---------------------------------------------------------------------------

# Candidate weight levels per layer. Three levels over four layers is 81 combinations,
# each of which is a re-fusion of cached frames -- seconds, not minutes. A finer grid
# fits the noise in a single fold rather than finding a better model.
GRID_LEVELS: tuple[float, ...] = (0.15, 0.30, 0.45)


def weight_grid(levels: Sequence[float] = GRID_LEVELS) -> list[LayerWeights]:
    return [
        LayerWeights(technical=t, macro=m, sentiment=s, positioning=p)
        for t, m, s, p in itertools.product(levels, repeat=4)
    ]


def fit_weights(
    frames: Sequence[Frame],
    history: History,
    settings: Settings,
    horizon: int = DEFAULT_HORIZON,
    grid: Sequence[LayerWeights] | None = None,
) -> tuple[LayerWeights, dict[str, object]]:
    """Pick the weight set with the best in-sample objective on these frames.

    In-sample by definition -- that is what a training fold is. The result is only
    meaningful after ``walk_forward`` has evaluated it on the following fold, which is
    why this function is not exported as a way to "tune the model".
    """
    if history.synthetic:
        log.warning(
            "fitting weights on SYNTHETIC data: the result describes the generator, not "
            "any market. Use it to exercise the code path, never to configure a deployment."
        )
    candidates = list(grid or weight_grid())
    best: tuple[float, LayerWeights, list[Trade]] | None = None
    for candidate in candidates:
        trades = grade_frames(frames, history, settings, candidate, horizon)
        value = objective(trades)
        if best is None or value > best[0]:
            best = (value, candidate, trades)
    assert best is not None
    value, weights, trades = best
    summary = dict(metrics(trades))
    summary["objective"] = value
    summary["candidates"] = len(candidates)
    return weights, summary


@dataclass
class Fold:
    """One train/test split of the walk-forward, with both sides' numbers kept.

    Keeping the in-sample numbers is not padding: the gap between train and test
    expectancy is the overfitting measure, and a fold where train is excellent and test
    is negative is the most informative output this module produces.
    """

    index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    weights: LayerWeights
    train_metrics: dict[str, object]
    test_metrics: dict[str, object]
    test_trades: list[Trade] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "train_start": self.train_start.isoformat(),
            "train_end": self.train_end.isoformat(),
            "test_start": self.test_start.isoformat(),
            "test_end": self.test_end.isoformat(),
            "weights": self.weights.as_dict(),
            "train": self.train_metrics,
            "test": self.test_metrics,
        }


@dataclass
class WalkForward:
    """The out-of-sample result. ``oos_metrics`` is the only number fit to publish."""

    asset: str
    folds: list[Fold]
    oos_metrics: dict[str, object]
    pooled_weights: LayerWeights
    synthetic: bool
    by_strength: dict[str, dict[str, object]] = field(default_factory=dict)
    by_regime: dict[str, dict[str, object]] = field(default_factory=dict)

    @property
    def overfit_gap(self) -> float | None:
        """Mean in-sample expectancy minus mean out-of-sample expectancy.

        The number to watch. A large positive gap means the fitting is finding noise; it
        does not invalidate the system, but it does mean the published expectancy should
        be the OOS one and the weights should be fit on longer windows.
        """
        trains = [
            f.train_metrics.get("expectancy_r") for f in self.folds
        ]
        tests = [f.test_metrics.get("expectancy_r") for f in self.folds]
        trains = [t for t in trains if isinstance(t, (int, float))]
        tests = [t for t in tests if isinstance(t, (int, float))]
        if not trains or not tests:
            return None
        return sum(trains) / len(trains) - sum(tests) / len(tests)

    def as_dict(self) -> dict[str, object]:
        return {
            "asset": self.asset,
            "synthetic": self.synthetic,
            "folds": [fold.as_dict() for fold in self.folds],
            "oos": self.oos_metrics,
            "pooled_weights": self.pooled_weights.as_dict(),
            "overfit_gap_r": self.overfit_gap,
            "by_strength": self.by_strength,
            "by_regime": self.by_regime,
        }


def walk_forward(
    history: History,
    settings: Settings,
    scorer: NewsScorer | None = None,
    train_bars: int = 250,
    test_bars: int = 60,
    warmup: int = WARMUP,
    horizon: int = DEFAULT_HORIZON,
    step: int = 1,
    grid: Sequence[LayerWeights] | None = None,
) -> WalkForward:
    """Roll a train/test window forward through the history.

    Defaults of 250 training bars and 60 testing bars on a daily series are about a
    trading year in, a quarter out. Shorter training windows fit noise; longer test
    windows hide a regime change inside one fold's average.

    ``train_bars`` and ``test_bars`` count *scored frames*, which equal bars only when
    ``step`` is 1. With ``step=3`` a 250-frame training window spans 750 bars of history --
    worth remembering when a thinned pass reports "not enough history".
    """
    frames = build_frames(history, settings, scorer, warmup=warmup, step=step)
    if len(frames) < train_bars + test_bars:
        raise ValueError(
            f"{history.asset}: {len(frames)} scored bars is not enough for a "
            f"{train_bars}+{test_bars} walk-forward; need "
            f"{train_bars + test_bars + warmup} bars of history"
        )

    folds: list[Fold] = []
    oos: list[Trade] = []
    cursor = 0
    index = 0
    while cursor + train_bars + test_bars <= len(frames):
        train = frames[cursor : cursor + train_bars]
        test = frames[cursor + train_bars : cursor + train_bars + test_bars]
        weights, train_metrics = fit_weights(train, history, settings, horizon, grid)
        test_trades = grade_frames(test, history, settings, weights, horizon)
        folds.append(
            Fold(
                index=index,
                train_start=train[0].ts,
                train_end=train[-1].ts,
                test_start=test[0].ts,
                test_end=test[-1].ts,
                weights=weights,
                train_metrics=train_metrics,
                test_metrics=dict(metrics(test_trades)),
                test_trades=test_trades,
            )
        )
        oos.extend(test_trades)
        cursor += test_bars
        index += 1

    pooled = _average_weights([fold.weights for fold in folds])
    return WalkForward(
        asset=history.asset,
        folds=folds,
        oos_metrics=dict(metrics(oos)),
        pooled_weights=pooled,
        synthetic=history.synthetic,
        by_strength=group_metrics(oos, lambda t: t.strength),
        by_regime=group_metrics(oos, lambda t: t.regime),
    )


def _average_weights(sets: Sequence[LayerWeights]) -> LayerWeights:
    """Mean of the per-fold fitted weights, as the deployment candidate.

    Averaging rather than taking the last fold's: the last fold is one quarter of data
    and its weights are noisy. A mean over folds is the closest thing available to a
    stable estimate, and the walk-forward's OOS metrics already say how much to trust it.
    """
    if not sets:
        return LayerWeights(0.25, 0.25, 0.25, 0.25)
    return LayerWeights(
        **{
            layer: sum(getattr(w, layer) for w in sets) / len(sets)
            for layer in LAYERS
        }
    )


def group_metrics(trades: Sequence[Trade], key) -> dict[str, dict[str, object]]:
    buckets: dict[str, list[Trade]] = {}
    for trade in trades:
        buckets.setdefault(str(key(trade)), []).append(trade)
    return {name: dict(metrics(group)) for name, group in sorted(buckets.items())}


def persist(store, result: WalkForward, interval: str = "1day") -> list[str]:
    """Write a walk-forward's folds and its pooled result to ``backtest_runs``."""
    ids: list[str] = []
    for fold in result.folds:
        ids.append(
            store.write_backtest_run(
                {
                    "id": uuid.uuid4().hex,
                    "asset": result.asset,
                    "interval": interval,
                    "kind": "walk_forward_fold",
                    "train_start": fold.train_start.isoformat(),
                    "train_end": fold.train_end.isoformat(),
                    "test_start": fold.test_start.isoformat(),
                    "test_end": fold.test_end.isoformat(),
                    "weights": fold.weights.as_dict(),
                    "metrics": {"train": fold.train_metrics, "test": fold.test_metrics},
                    "sample_size": int(fold.test_metrics.get("trades") or 0),
                }
            )
        )
    ids.append(
        store.write_backtest_run(
            {
                "id": uuid.uuid4().hex,
                "asset": result.asset,
                "interval": interval,
                "kind": "walk_forward_oos",
                "train_start": (
                    result.folds[0].train_start.isoformat() if result.folds else None
                ),
                "train_end": result.folds[-1].train_end.isoformat() if result.folds else None,
                "test_start": result.folds[0].test_start.isoformat() if result.folds else None,
                "test_end": result.folds[-1].test_end.isoformat() if result.folds else None,
                "weights": result.pooled_weights.as_dict(),
                "metrics": {
                    "oos": result.oos_metrics,
                    "by_strength": result.by_strength,
                    "by_regime": result.by_regime,
                    "overfit_gap_r": result.overfit_gap,
                    "synthetic": result.synthetic,
                },
                "sample_size": int(result.oos_metrics.get("trades") or 0),
            }
        )
    )
    return ids


def history_from_feeds(
    asset: str,
    feeds,
    interval: str = "1day",
    bars: int = 1200,
    now: datetime | None = None,
) -> History:
    """Assemble a ``History`` from live or synthetic feeds.

    News history is the honest gap here: Finnhub's free tier serves recent headlines
    only, so a multi-year backtest has no sentiment layer for most of its span. That is
    reported rather than papered over -- the sentiment layer scores unavailable for those
    bars and its weight redistributes, exactly as it would live during a feed outage.
    """
    now = now or datetime.now(timezone.utc)
    series = feeds.prices.candles(asset, interval, bars).closed(now)
    macro: dict[str, MacroSeries] = {}
    if feeds.macro is not None:
        from .providers import macro_series_ids

        for series_id in macro_series_ids():
            try:
                macro[series_id] = feeds.macro.series(series_id, 2000)
            except Exception as exc:
                log.warning("%s: macro series %s unavailable: %s", asset, series_id, exc)
    cot = None
    if feeds.positioning is not None:
        try:
            cot = feeds.positioning.history(asset, 400)
        except Exception as exc:
            log.warning("%s: COT history unavailable: %s", asset, exc)
    headlines: list[Headline] = []
    if feeds.news is not None:
        try:
            headlines = list(feeds.news.headlines(now.replace(year=now.year - 2), 500))
        except Exception as exc:
            log.warning("%s: news history unavailable: %s", asset, exc)

    return History(
        asset=asset,
        series=series,
        macro=macro,
        cot=cot,
        headlines=headlines,
        synthetic=feeds.synthetic,
    )


def history_from_store(store, asset: str, interval: str = "1day", bars: int = 1200) -> History:
    """Replay from what has already been collected. No network, no quota."""
    return History(asset=asset, series=store.load_candles(asset, interval, bars))
