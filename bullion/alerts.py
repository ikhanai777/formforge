"""When to interrupt someone's day, and when to keep quiet.

Spec section 6: alert on signal *change*, not on every recompute -- "nobody wants a
ping every 5 minutes". That one line implies everything in this module, because the
naive implementation (alert when the state string differs from last time) pings
constantly: a composite oscillating around the BUY/HOLD boundary flips state every
cycle, and each flip looks like news.

So three defences, in order of how much they matter:

* **Hysteresis.** A state change only alerts if it is a *direction* change, a strength
  change, or a move in or out of NO_SIGNAL. BUY -> HOLD -> BUY inside the cooldown is
  one alert, not three.
* **Cooldown.** Per asset per kind, from ``Thresholds.alert_cooldown_minutes``, read from
  the alerts table so it survives a restart -- a process that restarts every deploy
  would otherwise re-alert on its first cycle every time.
* **Confidence band, not confidence value.** Confidence drifts continuously; alerting on
  a threshold crossing means alerting repeatedly as it wobbles over the line. The
  crossing has to be by at least ``alert_confidence_delta`` from the last alerted value.

Invalidation alerts (the stop being hit) are the exception to all of this: they fire
immediately and ignore the cooldown, because the whole point of publishing an
invalidation level is telling the user when it is reached.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Thresholds
from .disclosure import SHORT_DISCLOSURE
from .fusion import Signal
from .series import Series
from .store import SignalRow, Store

log = logging.getLogger("bullion.alerts")

# Alert kinds, in the order a user would rank them.
KIND_INVALIDATED = "invalidated"
KIND_TARGET = "target_reached"
KIND_NEW = "new_signal"
KIND_FLIP = "direction_change"
KIND_CONFIDENCE = "confidence_shift"
KIND_SILENT = "signal_withdrawn"


@dataclass(frozen=True)
class Alert:
    """One notification, ready to deliver. ``body`` is user-facing copy."""

    kind: str
    asset: str
    title: str
    body: str
    signal_id: str | None = None
    priority: str = "normal"
    ts: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.ts is None:
            object.__setattr__(self, "ts", datetime.now(timezone.utc))

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "asset": self.asset,
            "title": self.title,
            "body": self.body,
            "signal_id": self.signal_id,
            "priority": self.priority,
            "ts": self.ts.isoformat(),
            "disclosure": SHORT_DISCLOSURE,
        }


def _cooled_down(
    store: Store | None, asset: str, kind: str, minutes: int, now: datetime
) -> bool:
    if store is None:
        return True
    last = store.last_alert(asset, kind)
    if not last:
        return True
    try:
        when = datetime.fromisoformat(last["ts"])
    except (KeyError, ValueError):  # pragma: no cover - malformed row
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return now - when >= timedelta(minutes=minutes)


def _materially_different(previous: SignalRow | None, signal: Signal) -> tuple[bool, str]:
    """Whether the change is worth a notification, and which kind it is.

    Returns ``(False, reason)`` for the oscillation cases, which is most of them.
    """
    if previous is None:
        # A first read of HOLD or NO_SIGNAL is not news. "We have started watching and
        # currently have no opinion" is not worth a push notification.
        if signal.direction == 0:
            return False, f"first read is {signal.state.value}; nothing to announce"
        return True, KIND_NEW

    if previous.state == signal.state.value:
        return False, "state unchanged"

    was_actionable = previous.direction != 0
    now_actionable = signal.direction != 0

    if was_actionable and now_actionable and previous.direction != signal.direction:
        # Long to short or back: always worth knowing.
        return True, KIND_FLIP
    if not was_actionable and now_actionable:
        return True, KIND_NEW
    if was_actionable and not now_actionable:
        return True, KIND_SILENT
    if previous.strength != signal.state.strength and now_actionable:
        # BUY -> STRONG_BUY is a real change in conviction.
        return True, KIND_NEW
    return False, f"{previous.state} -> {signal.state.value} is not a material change"


def check_invalidation(previous: SignalRow | None, series: Series) -> Alert | None:
    """Did the previous open signal's stop or target print since it was issued?

    Checked against bar *extremes*, not closes: a stop is hit intrabar, and grading it
    on closes would report a win on a trade that was stopped out hours earlier. When
    both levels are touched in the same bar the stop wins, because without tick data
    the order is unknowable and assuming the favourable one is how backtests lie.
    """
    if previous is None or previous.direction == 0:
        return None
    if previous.suggested_stop is None:
        return None

    after = series.after(previous.ts)
    if not len(after):
        return None

    long_ = previous.direction > 0
    stop, target = previous.suggested_stop, previous.suggested_target
    for candle in after:
        hit_stop = candle.low <= stop if long_ else candle.high >= stop
        hit_target = (
            target is not None and (candle.high >= target if long_ else candle.low <= target)
        )
        if hit_stop:
            return Alert(
                kind=KIND_INVALIDATED,
                asset=previous.asset,
                title=f"{previous.asset}: {previous.state} invalidated",
                body=(
                    f"The {previous.state} signal issued at {previous.price_at_signal:,.2f} "
                    f"reached its invalidation level of {stop:,.2f} on the bar opening "
                    f"{candle.ts:%Y-%m-%d %H:%M} UTC. The signal is closed and graded "
                    "as a loss."
                ),
                signal_id=previous.id,
                priority="high",
                ts=candle.ts,
            )
        if hit_target:
            return Alert(
                kind=KIND_TARGET,
                asset=previous.asset,
                title=f"{previous.asset}: {previous.state} target reached",
                body=(
                    f"The {previous.state} signal issued at {previous.price_at_signal:,.2f} "
                    f"reached its target of {target:,.2f} on the bar opening "
                    f"{candle.ts:%Y-%m-%d %H:%M} UTC."
                ),
                signal_id=previous.id,
                priority="normal",
                ts=candle.ts,
            )
    return None


def evaluate(
    signal: Signal,
    previous: SignalRow | None,
    series: Series | None = None,
    store: Store | None = None,
    thresholds: Thresholds | None = None,
    signal_id: str | None = None,
    now: datetime | None = None,
) -> list[Alert]:
    """Decide what to notify about this cycle. Usually nothing, which is the point."""
    thresholds = thresholds or Thresholds()
    now = now or datetime.now(timezone.utc)
    out: list[Alert] = []

    # Invalidation first and unconditionally -- no cooldown, no hysteresis.
    if series is not None:
        hit = check_invalidation(previous, series)
        if hit is not None:
            out.append(hit)

    material, kind = _materially_different(previous, signal)
    if material:
        if _cooled_down(store, signal.asset, kind, thresholds.alert_cooldown_minutes, now):
            out.append(
                Alert(
                    kind=kind,
                    asset=signal.asset,
                    title=signal.headline(),
                    body=_body_for(kind, signal, previous),
                    signal_id=signal_id,
                    priority="high" if kind in (KIND_FLIP, KIND_NEW) else "normal",
                    ts=now,
                )
            )
        else:
            log.debug("%s: suppressing %s alert, still in cooldown", signal.asset, kind)
    elif previous is not None and signal.direction != 0:
        # Same state, but confidence may have moved enough to matter.
        moved = abs(signal.confidence.value - previous.confidence)
        if moved >= thresholds.alert_confidence_delta and _cooled_down(
            store, signal.asset, KIND_CONFIDENCE, thresholds.alert_cooldown_minutes, now
        ):
            direction = "rose" if signal.confidence.value > previous.confidence else "fell"
            out.append(
                Alert(
                    kind=KIND_CONFIDENCE,
                    asset=signal.asset,
                    title=(
                        f"{signal.asset} {signal.state.value}: confidence {direction} to "
                        f"{signal.confidence.value * 100:.0f}%"
                    ),
                    body=(
                        f"Still {signal.state.value}, but confidence {direction} from "
                        f"{previous.confidence * 100:.0f}% to "
                        f"{signal.confidence.value * 100:.0f}%. {signal.confidence.explain()}."
                    ),
                    signal_id=signal_id,
                    ts=now,
                )
            )

    return out


def _body_for(kind: str, signal: Signal, previous: SignalRow | None) -> str:
    """User-facing copy. Descriptive, never imperative (spec section 11)."""
    levels = ""
    if signal.levels.stop is not None and signal.levels.target is not None:
        levels = (
            f" Suggested invalidation {signal.levels.stop:,.2f}, "
            f"target {signal.levels.target:,.2f} ({signal.levels.basis})."
        )
    reasons = "; ".join(
        layer.as_text() for layer in signal.layers.values() if layer.available
    )
    if kind == KIND_SILENT:
        return (
            f"The previous {previous.state if previous else 'signal'} no longer holds: the "
            f"composite score is {signal.composite_score:+.2f} at "
            f"{signal.confidence.value * 100:.0f}% confidence, which the engine reports as "
            f"{signal.state.value} rather than a directional call. {reasons}"
        )
    prefix = (
        f"Direction changed from {previous.state} to {signal.state.value}."
        if kind == KIND_FLIP and previous
        else f"New signal: {signal.state.value}."
    )
    return (
        f"{prefix} {signal.asset} at {signal.price_at_signal:,.2f}, confidence "
        f"{signal.confidence.value * 100:.0f}% ({signal.confidence.explain()})."
        f"{levels} {reasons}"
    )


def deliver(alerts: list[Alert], store: Store | None = None) -> int:
    """Record alerts and hand them to whatever transport is configured.

    There is no push transport wired up here on purpose: APNs/FCM credentials are a
    deployment concern, and a stub that pretends to send is worse than an honest
    persistence-plus-log. The alerts table is the queue a delivery worker reads.
    """
    for alert in alerts:
        log.info("ALERT [%s] %s", alert.kind, alert.title)
        if store is not None:
            store.record_alert(
                asset=alert.asset,
                kind=alert.kind,
                title=alert.title,
                body=alert.body,
                signal_id=alert.signal_id,
                ts=alert.ts,
                delivered=False,
            )
    return len(alerts)
