"""The self-graded track record, and the drift check against the backtest.

Spec section 5: "Publish a live, auto-updating accuracy scorecard inside the app -- win
rate by signal strength, by regime, by metal. This is the actual trust mechanism, more
than any marketing claim of 'highly accurate' would be."

This module is that, and it is written to be unflattering by default:

* **Every win rate ships with a Wilson interval and a sample size.** 62% from 13 signals
  and 62% from 400 signals are different claims. The API returns both numbers and the
  dashboard renders the interval, so a thin record looks thin.
* **Open signals are excluded, not counted.** A signal whose horizon has not elapsed is
  pending. Counting pending signals as wins inflates a rising market; counting them as
  losses understates a new deployment. Either is a lie available for free, so the
  scorecard reports ``pending`` as its own number.
* **Synthetic signals are segregated.** A demo run writes rows like any other; a
  scorecard that pooled them with live ones would be fiction. ``include_synthetic``
  defaults to False and the payload says how many rows were excluded.
* **Drift is tested, not eyeballed.** Live win rate versus the backtested expectation is
  a two-proportion z-test; below 20 resolved signals it reports "insufficient sample"
  rather than a reassuring number. Spec section 5 wants drift alerting, and drift that
  fires on eight trades is noise with a siren attached.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .backtest import DEFAULT_HORIZON, wilson_interval
from .config import ASSETS
from .series import Series
from .store import SignalRow, Store

log = logging.getLogger("bullion.scorecard")

# Minimum resolved signals before a drift verdict is offered at all.
MIN_DRIFT_SAMPLE = 20
# z at which drift is called. 2.0 is about 95% two-sided.
DRIFT_Z = 2.0


# ---------------------------------------------------------------------------
# Grading stored signals
# ---------------------------------------------------------------------------


def grade_pending(
    store: Store,
    assets: Sequence[str] = ASSETS,
    interval: str = "1day",
    horizon: int = DEFAULT_HORIZON,
    now: datetime | None = None,
) -> dict[str, int]:
    """Resolve every ungraded stored signal against the bars that followed it.

    Idempotent and safe to run on a schedule: a signal is graded once its horizon has
    fully elapsed, and re-running only picks up newly-matured ones. The bars come from the
    store, not the provider, so grading costs no API quota and grades against exactly the
    prices the signal was issued on.
    """
    now = now or datetime.now(timezone.utc)
    counts = {"graded": 0, "pending": 0, "unresolvable": 0}

    for asset in assets:
        series = store.load_candles(asset, interval, 5000)
        if not len(series):
            continue
        for row in store.ungraded_signals(asset):
            outcome = _resolve_row(row, series, horizon, now)
            if outcome is None:
                counts["pending"] += 1
                continue
            if outcome == "unresolvable":
                counts["unresolvable"] += 1
                continue
            store.write_outcome(outcome)
            counts["graded"] += 1
    return counts


def _resolve_row(
    row: SignalRow,
    series: Series,
    horizon: int,
    now: datetime,
) -> dict[str, Any] | str | None:
    """Grade one stored signal. None means still open; "unresolvable" means no levels."""
    if row.direction == 0 or row.suggested_stop is None or row.suggested_target is None:
        return "unresolvable"

    future = series.after(row.ts)
    if not len(future):
        return None

    bars = future.candles[:horizon]
    entry = row.price_at_signal
    stop = row.suggested_stop
    target = row.suggested_target
    risk = abs(entry - stop)
    if risk <= 0:
        return "unresolvable"
    long_ = row.direction > 0

    outcome = None
    exit_price = bars[-1].close
    bars_held = len(bars)
    mfe = mae = 0.0

    for index, candle in enumerate(bars, start=1):
        mfe = max(mfe, ((candle.high - entry) if long_ else (entry - candle.low)) / risk)
        mae = max(mae, ((entry - candle.low) if long_ else (candle.high - entry)) / risk)
        hit_stop = candle.low <= stop if long_ else candle.high >= stop
        hit_target = candle.high >= target if long_ else candle.low <= target
        if hit_stop:
            outcome, exit_price, bars_held = "stop", stop, index
            break
        if hit_target:
            outcome, exit_price, bars_held = "target", target, index
            break

    if outcome is None:
        # Not stopped or targeted yet. Only a *matured* horizon counts as a timeout;
        # otherwise the signal is still open and grading it now would bias the record
        # toward whatever the market happened to be doing today.
        if len(bars) < horizon:
            return None
        outcome = "timeout"

    direction = 1 if long_ else -1

    def price_at(offset: int) -> float | None:
        return bars[offset - 1].close if len(bars) >= offset else None

    def signed(price: float | None) -> float | None:
        return direction * (price - entry) / entry if price is not None and entry else None

    return {
        "signal_id": row.id,
        "resolved_at": now.isoformat(),
        "outcome": outcome,
        "bars_held": bars_held,
        "exit_price": exit_price,
        "price_1d": price_at(1),
        "price_3d": price_at(3),
        "price_7d": price_at(7),
        "return_1d": signed(price_at(1)),
        "return_3d": signed(price_at(3)),
        "return_7d": signed(price_at(7)),
        "mfe_r": mfe,
        "mae_r": mae,
        "notes": None,
    }


# ---------------------------------------------------------------------------
# The scorecard itself
# ---------------------------------------------------------------------------


@dataclass
class Bucket:
    """One row of the scorecard."""

    name: str
    trades: int
    wins: int
    win_rate: float | None
    win_rate_ci: tuple[float, float]
    expectancy_r: float | None
    avg_return_7d: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "trades": self.trades,
            "wins": self.wins,
            "win_rate": round(self.win_rate, 4) if self.win_rate is not None else None,
            "win_rate_ci": [round(self.win_rate_ci[0], 4), round(self.win_rate_ci[1], 4)],
            "expectancy_r": (
                round(self.expectancy_r, 4) if self.expectancy_r is not None else None
            ),
            "avg_return_7d": (
                round(self.avg_return_7d, 5) if self.avg_return_7d is not None else None
            ),
            # Rendered by the dashboard next to the number, so a thin record reads as thin.
            "reliable": self.trades >= MIN_DRIFT_SAMPLE,
        }


def _bucket(name: str, rows: Sequence[dict[str, Any]]) -> Bucket:
    n = len(rows)
    if n == 0:
        return Bucket(name, 0, 0, None, (0.0, 1.0), None, None)
    wins = sum(1 for row in rows if _is_win(row))
    rs = [row["r_multiple"] for row in rows if row.get("r_multiple") is not None]
    sevens = [row["return_7d"] for row in rows if row.get("return_7d") is not None]
    return Bucket(
        name=name,
        trades=n,
        wins=wins,
        win_rate=wins / n,
        win_rate_ci=wilson_interval(wins, n),
        expectancy_r=(sum(rs) / len(rs)) if rs else None,
        avg_return_7d=(sum(sevens) / len(sevens)) if sevens else None,
    )


def _is_win(row: dict[str, Any]) -> bool:
    """A win is the target being reached, or a positive timeout. A stop is a loss.

    Defined on the *outcome*, not on the sign of the 7-day return, because a trade that
    was stopped out on day two and then recovered by day seven was a loss -- the user was
    out of it. Grading on the later price is the most flattering error available.
    """
    if row["outcome"] == "target":
        return True
    if row["outcome"] == "stop":
        return False
    value = row.get("r_multiple")
    if value is None:
        value = row.get("return_7d") or 0.0
    return value > 0


@dataclass
class Scorecard:
    """The published record. ``overall`` is the headline; the buckets are the honesty."""

    generated_at: datetime
    overall: Bucket
    by_asset: dict[str, Bucket]
    by_strength: dict[str, Bucket]
    by_regime: dict[str, Bucket]
    by_confidence: dict[str, Bucket]
    pending: int
    excluded_synthetic: int
    horizon_bars: int = DEFAULT_HORIZON

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "horizon_bars": self.horizon_bars,
            "overall": self.overall.as_dict(),
            "by_asset": {k: v.as_dict() for k, v in self.by_asset.items()},
            "by_strength": {k: v.as_dict() for k, v in self.by_strength.items()},
            "by_regime": {k: v.as_dict() for k, v in self.by_regime.items()},
            "by_confidence": {k: v.as_dict() for k, v in self.by_confidence.items()},
            "pending": self.pending,
            "excluded_synthetic": self.excluded_synthetic,
            "caveat": (
                "Win rate is measured on signals this engine actually issued and graded "
                "against subsequent prices, stop-first on ties. Confidence intervals are "
                "Wilson 95%. A bucket with fewer than "
                f"{MIN_DRIFT_SAMPLE} graded signals is not a reliable estimate of anything."
            ),
        }


def _confidence_bucket(value: float) -> str:
    if value >= 0.75:
        return "0.75+"
    if value >= 0.6:
        return "0.60-0.75"
    if value >= 0.45:
        return "0.45-0.60"
    return "below 0.45"


def build(
    store: Store,
    include_synthetic: bool = False,
    since: datetime | None = None,
    horizon: int = DEFAULT_HORIZON,
) -> Scorecard:
    """Compute the scorecard from graded rows in the store."""
    rows = store.graded()
    excluded = 0
    if not include_synthetic:
        before = len(rows)
        rows = [row for row in rows if not row["synthetic"]]
        excluded = before - len(rows)
    if since is not None:
        rows = [row for row in rows if datetime.fromisoformat(row["ts"]) >= since]

    # Derive the R multiple from the stored outcome where the column is absent: the
    # signals table holds the stop, so risk is recoverable and every bucket can report in R.
    for row in rows:
        row.setdefault("r_multiple", None)
        if row["r_multiple"] is None:
            row["r_multiple"] = _infer_r(row, store)

    pending = len(store.ungraded_signals())

    return Scorecard(
        generated_at=datetime.now(timezone.utc),
        overall=_bucket("overall", rows),
        by_asset=_group(rows, lambda row: row["asset"]),
        by_strength=_group(rows, lambda row: row["strength"]),
        by_regime=_group(rows, lambda row: row["regime"]),
        by_confidence=_group(rows, lambda row: _confidence_bucket(row["confidence"])),
        pending=pending,
        excluded_synthetic=excluded,
        horizon_bars=horizon,
    )


def _group(
    rows: Sequence[dict[str, Any]], key: Callable[[dict[str, Any]], str]
) -> dict[str, Bucket]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(str(key(row)), []).append(row)
    return {name: _bucket(name, group) for name, group in sorted(buckets.items())}


def _infer_r(row: dict[str, Any], store: Store) -> float | None:
    """Recover the R multiple of a graded row from its stored stop.

    Stored separately from the outcome because the outcomes table records prices; R needs
    the risk, which lives on the signal. One join's worth of work to keep every bucket
    reported in the same unit as the backtest.
    """
    with store.connection() as conn:
        signal = conn.execute(
            "SELECT price_at_signal, suggested_stop FROM signals WHERE id=?", (row["id"],)
        ).fetchone()
    if signal is None or signal["suggested_stop"] is None:
        return None
    entry = signal["price_at_signal"]
    risk = abs(entry - signal["suggested_stop"])
    exit_price = row.get("exit_price")
    if not risk or exit_price is None:
        return None
    direction = 1 if row["state"] in ("BUY", "STRONG_BUY") else -1
    return direction * (exit_price - entry) / risk


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------


@dataclass
class Drift:
    """Whether live performance still matches what the backtest predicted.

    Spec section 5 notes that drift is itself useful information -- it usually means a
    regime change rather than a broken model. So the verdict distinguishes "worse than
    backtested" from "better than backtested", and both are reported as drift.
    """

    live_trades: int
    live_win_rate: float | None
    expected_win_rate: float | None
    z: float | None
    verdict: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "live_trades": self.live_trades,
            "live_win_rate": (
                round(self.live_win_rate, 4) if self.live_win_rate is not None else None
            ),
            "expected_win_rate": (
                round(self.expected_win_rate, 4) if self.expected_win_rate is not None else None
            ),
            "z": round(self.z, 2) if self.z is not None else None,
            "verdict": self.verdict,
            "detail": self.detail,
        }


def drift(
    live: Bucket,
    expected_win_rate: float | None,
    expected_trades: int = 0,
) -> Drift:
    """Two-proportion z-test of live win rate against the backtested expectation."""
    if expected_win_rate is None:
        return Drift(
            live.trades,
            live.win_rate,
            None,
            None,
            "no_baseline",
            "no backtested win rate to compare against; run a walk-forward first",
        )
    if live.trades < MIN_DRIFT_SAMPLE:
        return Drift(
            live.trades,
            live.win_rate,
            expected_win_rate,
            None,
            "insufficient_sample",
            (
                f"{live.trades} graded signals is below the {MIN_DRIFT_SAMPLE} needed to "
                "distinguish drift from noise"
            ),
        )

    p_live = live.win_rate or 0.0
    n_live = live.trades
    # Pool against the backtest sample when its size is known; otherwise treat the
    # backtested rate as a fixed expectation, which is the conservative reading.
    if expected_trades >= MIN_DRIFT_SAMPLE:
        pooled = (p_live * n_live + expected_win_rate * expected_trades) / (
            n_live + expected_trades
        )
        se = math.sqrt(pooled * (1 - pooled) * (1 / n_live + 1 / expected_trades))
    else:
        se = math.sqrt(max(1e-9, expected_win_rate * (1 - expected_win_rate) / n_live))
    z = (p_live - expected_win_rate) / se if se else 0.0

    if abs(z) < DRIFT_Z:
        verdict = "in_line"
        detail = (
            f"live {p_live * 100:.0f}% over {n_live} signals is within noise of the "
            f"backtested {expected_win_rate * 100:.0f}% (z={z:+.2f})"
        )
    elif z < 0:
        verdict = "underperforming"
        detail = (
            f"live {p_live * 100:.0f}% over {n_live} signals is below the backtested "
            f"{expected_win_rate * 100:.0f}% (z={z:+.2f}); re-fit the weights and check "
            "whether the macro regime has changed"
        )
    else:
        verdict = "outperforming"
        detail = (
            f"live {p_live * 100:.0f}% over {n_live} signals is above the backtested "
            f"{expected_win_rate * 100:.0f}% (z={z:+.2f}); pleasant, and still drift -- the "
            "backtest is no longer describing the live system"
        )
    return Drift(n_live, p_live, expected_win_rate, z, verdict, detail)


def drift_from_store(store: Store, asset: str | None = None) -> Drift:
    """Compare the live record to the most recent stored walk-forward for an asset."""
    card = build(store)
    live = card.by_asset.get(asset, card.overall) if asset else card.overall
    runs = [
        run
        for run in store.backtest_runs(asset)
        if run["kind"] == "walk_forward_oos"
    ]
    if not runs:
        return drift(live, None)
    oos = (runs[0]["metrics"] or {}).get("oos", {})
    if runs[0]["metrics"].get("synthetic"):
        return Drift(
            live.trades,
            live.win_rate,
            None,
            None,
            "no_baseline",
            "the most recent walk-forward ran on synthetic data and is not a baseline",
        )
    return drift(live, oos.get("win_rate"), int(oos.get("trades") or 0))


def recent_activity(store: Store, days: int = 30) -> dict[str, Any]:
    """Signal counts by state over a window. Feeds the dashboard's activity strip."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = store.signals(since=since, limit=5000)
    by_state: dict[str, int] = {}
    for row in rows:
        by_state[row.state] = by_state.get(row.state, 0) + 1
    return {
        "days": days,
        "signals": len(rows),
        "by_state": dict(sorted(by_state.items())),
        "actionable": sum(1 for row in rows if row.direction != 0),
    }
