"""Tier gating: what the free tier is actually missing, enforced server-side.

Spec section 9 sells the free tier as delayed, gold-only and without the reasoning
breakdown. The only honest way to implement that is to withhold the data before it
leaves the server. A client-side blur over a full payload is not a tier, it is a
disclosure of the thing you are charging for.

Three things are withheld, and each is withheld in a way the client can *explain* rather
than silently lack -- every gated field is replaced by a short ``locked`` marker naming
what the paid tier adds. A paywall the user does not understand reads as a bug.

The delay is applied by serving the newest signal **older than** the lag, not by serving
the current signal with a stale timestamp. That distinction matters: the free tier sees a
real call that was real four hours ago, which is a genuine product. Faking the timestamp
would be fraud.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import TIERS, Tier
from ..store import SignalRow

LOCKED = {
    "reasoning": "Upgrade for the four-layer reasoning breakdown behind this signal.",
    "levels": "Upgrade for suggested entry, invalidation and target levels.",
    "layers": "Upgrade for per-layer scores, components and freshness.",
}


def resolve(name: str | None) -> Tier:
    """Look up a tier by name, defaulting to free. Unknown names do not get a pass."""
    return TIERS.get((name or "free").strip().lower(), TIERS["free"])


def visible_assets(tier: Tier, requested: Sequence[str] | None = None) -> list[str]:
    allowed = list(tier.assets)
    if requested is None:
        return allowed
    return [asset for asset in requested if asset in set(allowed)]


def pick_signal(
    rows: Sequence[SignalRow],
    tier: Tier,
    now: datetime | None = None,
) -> SignalRow | None:
    """Newest signal this tier is allowed to see.

    ``rows`` must be newest-first, as ``Store.signals`` returns them.
    """
    if tier.delay_minutes <= 0:
        return rows[0] if rows else None
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(minutes=tier.delay_minutes)
    for row in rows:
        if row.ts <= cutoff:
            return row
    return None


def apply(payload: dict[str, Any], tier: Tier) -> dict[str, Any]:
    """Strip what this tier does not include, leaving an explanation in its place."""
    out = dict(payload)
    out["tier"] = tier.name
    out["delayed_minutes"] = tier.delay_minutes

    if not tier.reasoning:
        out["reasoning"] = {"locked": LOCKED["reasoning"]}
        out["layers"] = {"locked": LOCKED["layers"]}
        # Confidence stays -- it is the headline of the product and withholding it would
        # leave the free tier with a bare word and no way to judge it. The *why* is the
        # paid feature, not the number.
        out.pop("confidence_detail", None)
        out["levels"] = {"locked": LOCKED["levels"]}
        out["suggested_stop"] = None
        out["suggested_target"] = None

    if not tier.track_record:  # pragma: no cover - every current tier includes it
        out.pop("track_record", None)

    return out


def describe(tier: Tier) -> dict[str, Any]:
    """What this tier includes, for the upgrade panel."""
    return {
        "name": tier.name,
        "delay_minutes": tier.delay_minutes,
        "assets": list(tier.assets),
        "reasoning_breakdown": tier.reasoning,
        "track_record": tier.track_record,
        "push_alerts": tier.alerts,
        "api_access": tier.api_access,
    }
