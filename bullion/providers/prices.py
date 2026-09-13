"""Price feeds: Twelve Data primary, GoldAPI and Metals-API as redundancy.

Spec section 3.1's recommendation, implemented as written: Twelve Data is primary
because one key also covers DXY and the Treasury-yield proxies the macro layer
needs, which collapses three accounts into one; GoldAPI is the cross-check.

The interesting code here is not the parsing, it is ``CrossValidatedPrices``.
Price feeds rarely 500 -- they go *stale*, returning a plausible number that
stopped updating an hour ago, and a single-feed engine will happily score it. So
bars come from the primary and the latest spot is checked against a second
source; a disagreement beyond a threshold is recorded as a data-quality fact that
lowers confidence rather than an exception that kills the cycle. Half a percent
between two gold feeds is either a fast market or a broken feed, and in both cases
the right answer is "be less sure", not "crash" and not "pretend".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..series import Candle, Series, interval_seconds
from .base import NotConfigured, ProviderError, Quote, StaleData
from .http import HttpClient, MonthlyBudget

log = logging.getLogger("bullion.providers.prices")

# Twelve Data and GoldAPI disagree about how to name the metals.
_TD_SYMBOL = {"XAU/USD": "XAU/USD", "XAG/USD": "XAG/USD"}
_GOLDAPI_SYMBOL = {"XAU/USD": "XAU", "XAG/USD": "XAG"}
_METALS_SYMBOL = {"XAU/USD": "XAU", "XAG/USD": "XAG"}


def _parse_ts(raw: str) -> datetime:
    """Twelve Data returns naive local-exchange timestamps; treat them as UTC.

    Not strictly correct for equity sessions, but metals trade nearly around the
    clock and the engine only needs bars to be consistently ordered and spaced.
    Mixing naive and aware timestamps is the actual hazard, so everything is
    coerced here, once.
    """
    text = raw.strip().replace("T", " ").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ProviderError(f"unparseable timestamp {raw!r}")


@dataclass
class TwelveDataPrices:
    """Primary feed. One key covers metals, DXY and the yield proxies."""

    api_key: str
    client: HttpClient = field(
        default_factory=lambda: HttpClient("twelvedata", min_interval=8.0)
    )
    name: str = "twelvedata"
    base: str = "https://api.twelvedata.com"

    def __post_init__(self) -> None:
        if not self.api_key:
            raise NotConfigured("TWELVEDATA_API_KEY is not set")

    def candles(self, asset: str, interval: str = "1day", limit: int = 400) -> Series:
        symbol = _TD_SYMBOL.get(asset, asset)
        payload = self.client.get_json(
            f"{self.base}/time_series",
            {
                "symbol": symbol,
                "interval": interval,
                "outputsize": min(limit, 5000),
                "apikey": self.api_key,
                "order": "ASC",
                "timezone": "UTC",
            },
        )
        if isinstance(payload, dict) and payload.get("status") == "error":
            raise ProviderError(f"twelvedata: {payload.get('message', 'unknown error')}")
        values = (payload or {}).get("values") or []
        if not values:
            raise ProviderError(f"twelvedata: empty series for {symbol} {interval}")
        candles = [
            Candle(
                ts=_parse_ts(row["datetime"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume") or 0.0),
            )
            for row in values
        ]
        return Series.of(asset, interval, candles, source=self.name)

    def quote(self, asset: str) -> Quote:
        payload = self.client.get_json(
            f"{self.base}/price",
            {"symbol": _TD_SYMBOL.get(asset, asset), "apikey": self.api_key},
        )
        try:
            price = float(payload["price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(f"twelvedata: no price in {payload!r}") from exc
        return Quote(asset=asset, price=price, ts=datetime.now(timezone.utc), source=self.name)

    def macro_close_series(self, symbol: str, limit: int = 400) -> Series:
        """DXY and friends, as a price series. Used by the macro layer."""
        payload = self.client.get_json(
            f"{self.base}/time_series",
            {
                "symbol": symbol,
                "interval": "1day",
                "outputsize": min(limit, 5000),
                "apikey": self.api_key,
                "order": "ASC",
                "timezone": "UTC",
            },
        )
        values = (payload or {}).get("values") or []
        if not values:
            raise ProviderError(f"twelvedata: empty series for {symbol}")
        candles = [
            Candle(
                ts=_parse_ts(row["datetime"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume") or 0.0),
            )
            for row in values
        ]
        return Series.of(symbol, "1day", candles, source=self.name)


@dataclass
class GoldApiPrices:
    """Cross-check feed. 500 requests a month, so every call is budgeted.

    GoldAPI's historical endpoint is one request per day of history, which makes a
    400-bar backfill a budget-ending operation. It is therefore used for spot
    cross-validation only, and ``candles`` says so rather than quietly spending
    the month.
    """

    api_key: str
    budget_path: Path | None = None
    monthly_limit: int = 500
    name: str = "goldapi"
    base: str = "https://www.goldapi.io/api"
    client: HttpClient = field(init=False)

    def __post_init__(self) -> None:
        if not self.api_key:
            raise NotConfigured("GOLDAPI_KEY is not set")
        path = self.budget_path or Path.home() / ".bullion" / "budget-goldapi.json"
        self.client = HttpClient(
            "goldapi",
            budget=MonthlyBudget("goldapi", self.monthly_limit, path),
        )

    def _get(self, path: str) -> Any:
        # GoldAPI wants the key in a header; HttpClient is query-string only, so
        # the token rides in the path-style form the API also accepts.
        return self.client.get_json(f"{self.base}/{path}", {"api_key": self.api_key})

    def quote(self, asset: str) -> Quote:
        symbol = _GOLDAPI_SYMBOL.get(asset)
        if symbol is None:
            raise ProviderError(f"goldapi does not cover {asset}")
        payload = self._get(f"{symbol}/USD")
        try:
            price = float(payload["price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(f"goldapi: no price in {payload!r}") from exc
        ts = payload.get("timestamp")
        when = (
            datetime.fromtimestamp(float(ts), tz=timezone.utc)
            if ts
            else datetime.now(timezone.utc)
        )
        return Quote(
            asset=asset,
            price=price,
            ts=when,
            source=self.name,
            bid=_maybe_float(payload.get("bid")),
            ask=_maybe_float(payload.get("ask")),
        )

    def candles(self, asset: str, interval: str = "1day", limit: int = 400) -> Series:
        raise ProviderError(
            "goldapi historical is one request per day of history; use it for spot "
            "cross-validation only (see the class docstring)"
        )


@dataclass
class MetalsApiPrices:
    """Second redundancy option, and the only one of the three quoting bid/ask."""

    api_key: str
    name: str = "metalsapi"
    base: str = "https://metals-api.com/api"
    client: HttpClient = field(default_factory=lambda: HttpClient("metalsapi"))

    def __post_init__(self) -> None:
        if not self.api_key:
            raise NotConfigured("METALS_API_KEY is not set")

    def quote(self, asset: str) -> Quote:
        symbol = _METALS_SYMBOL.get(asset)
        if symbol is None:
            raise ProviderError(f"metals-api does not cover {asset}")
        payload = self.client.get_json(
            f"{self.base}/latest",
            {"access_key": self.api_key, "base": "USD", "symbols": symbol},
        )
        rates = (payload or {}).get("rates") or {}
        raw = rates.get(symbol)
        if raw in (None, 0):
            raise ProviderError(f"metals-api: no rate for {symbol} in {payload!r}")
        # Metals-API quotes USD->XAU, i.e. ounces per dollar. Invert.
        price = 1.0 / float(raw)
        ts = payload.get("timestamp")
        when = (
            datetime.fromtimestamp(float(ts), tz=timezone.utc)
            if ts
            else datetime.now(timezone.utc)
        )
        return Quote(asset=asset, price=price, ts=when, source=self.name)

    def candles(self, asset: str, interval: str = "1day", limit: int = 400) -> Series:
        raise ProviderError("metals-api is configured as a spot cross-check only")


def _maybe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class PriceQuality:
    """What the cross-check found. Lands in the signal payload verbatim.

    ``agreement`` is the confidence input: 1.0 when two feeds match, falling to 0
    at ``tolerance_bp`` x 4 of disagreement. ``sources`` names who was consulted,
    because "which feed said this" is the first question when a call looks wrong.
    """

    sources: tuple[str, ...]
    primary_price: float
    cross_price: float | None
    disagreement_bp: float | None
    agreement: float
    stale_seconds: float
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "sources": list(self.sources),
            "primary_price": self.primary_price,
            "cross_price": self.cross_price,
            "disagreement_bp": self.disagreement_bp,
            "agreement": self.agreement,
            "stale_seconds": self.stale_seconds,
            "note": self.note,
        }


@dataclass
class CrossValidatedPrices:
    """Bars from the primary feed, latest price sanity-checked against a second.

    ``max_stale_multiple`` is in bar lengths: a daily series whose last closed bar
    is three days old on a Wednesday is a broken backfill, not a quiet market, and
    scoring it produces a stale signal with a confident face on it.
    """

    primary: Any
    cross: Any | None = None
    tolerance_bp: float = 25.0
    max_stale_multiple: float = 3.0

    def candles(self, asset: str, interval: str = "1day", limit: int = 400) -> Series:
        return self.primary.candles(asset, interval, limit)

    def _allowance_bp(self, raw: Series, now: datetime) -> tuple[float, float]:
        """How far two feeds may legitimately differ right now, in basis points.

        This is the correction to the obvious implementation, which compares a live
        spot quote from one feed against the last *closed* bar from another and calls
        a 1% gap a broken feed. On a daily interval those two numbers are not supposed
        to match: one is now, the other is yesterday's close, and gold routinely moves
        1% in between. A fixed 25bp tolerance there produces a permanent false alarm.

        So the allowance is the fixed tolerance *plus* the market's own typical
        movement over the elapsed part of the current bar: median true range of the
        last 20 bars, scaled by how far into the bar we are. Inside that envelope the
        feeds are agreeing; outside it, one of them is wrong.

        Returns ``(allowance_bp, elapsed_fraction)``.
        """
        bar = interval_seconds(raw.interval)
        elapsed = min(1.0, max(0.0, (now - raw.last.ts).total_seconds()) / bar)
        recent = raw.candles[-21:-1] if len(raw) > 21 else raw.candles[:-1] or raw.candles
        ranges = sorted((c.high - c.low) / c.close * 10_000 for c in recent if c.close)
        typical = ranges[len(ranges) // 2] if ranges else 0.0
        return self.tolerance_bp + typical * elapsed, elapsed

    def validated(
        self,
        asset: str,
        interval: str = "1day",
        limit: int = 400,
        now: datetime | None = None,
    ) -> tuple[Series, PriceQuality]:
        now = now or datetime.now(timezone.utc)
        raw = self.primary.candles(asset, interval, limit)
        if not len(raw):
            raise ProviderError(f"{asset}: no bars from {self.primary.name}")
        # Scoring uses closed bars only; the cross-check uses the freshest price the
        # primary has, which is what a second feed's spot quote is comparable to.
        series = raw.closed(now)
        if not len(series):
            raise ProviderError(
                f"{asset}: {self.primary.name} returned {len(raw)} bars but none are closed"
            )

        age = series.age_seconds(now)
        allowed = interval_seconds(interval) * self.max_stale_multiple
        if age > allowed:
            raise StaleData(
                f"{asset}: last closed {interval} bar is {age / 3600:.1f}h old "
                f"(limit {allowed / 3600:.1f}h) from {self.primary.name}"
            )

        scored_price = series.last.close
        live_price = raw.last.close
        allowance, elapsed = self._allowance_bp(raw, now)

        cross_price: float | None = None
        disagreement: float | None = None
        note = f"single feed ({self.primary.name}); no cross-check configured"
        sources = [self.primary.name]

        if self.cross is not None:
            try:
                quote = self.cross.quote(asset)
                cross_price = quote.price
                sources.append(self.cross.name)
                disagreement = abs(cross_price - live_price) / live_price * 10_000
                if disagreement <= allowance:
                    note = (
                        f"{self.primary.name} and {self.cross.name} agree within "
                        f"{disagreement:.0f}bp (allowance {allowance:.0f}bp at "
                        f"{elapsed * 100:.0f}% through the bar)"
                    )
                else:
                    note = (
                        f"feeds disagree by {disagreement:.0f}bp, beyond the "
                        f"{allowance:.0f}bp allowance ({self.primary.name} "
                        f"{live_price:,.2f} vs {self.cross.name} {cross_price:,.2f}); "
                        "confidence reduced"
                    )
                    log.warning("%s: %s", asset, note)
            except ProviderError as exc:
                note = f"cross-check against {self.cross.name} unavailable: {exc}"
                log.warning("%s: %s", asset, note)

        if disagreement is None:
            # One feed is not nothing, but it is not two: cap the contribution so a
            # missing cross-check can never look as good as a confirmed one.
            agreement = 0.8
        else:
            excess = max(0.0, disagreement - allowance)
            agreement = max(0.0, 1.0 - excess / (allowance * 1.5)) if allowance else 0.0

        return series, PriceQuality(
            sources=tuple(sources),
            primary_price=scored_price,
            cross_price=cross_price,
            disagreement_bp=disagreement,
            agreement=agreement,
            stale_seconds=age,
            note=note,
        )

    def quote(self, asset: str) -> Quote:
        return self.primary.quote(asset)

    @property
    def name(self) -> str:
        names = [self.primary.name] + ([self.cross.name] if self.cross else [])
        return "+".join(names)
