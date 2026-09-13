"""Macro series from FRED, plus the dollar index from the price feed.

Spec section 3.3 calls real yields the single strongest gold driver, and that is
the one claim in this document I would defend without hedging: the 10-year TIPS
yield is the opportunity cost of holding a non-yielding asset, and gold's
multi-month moves are mostly that series inverted.

One correction to the spec while implementing it: FRED's JSON API *does* require a
free key (``api_key`` is mandatory on ``/fred/series/observations``). What needs no
key is the ``fredgraph.csv`` download that backs the public charts, so this
provider uses the CSV path when no key is configured and the JSON API when one is.
Both return the same series; the CSV path is rate-limited by politeness rather
than by quota, so it is fetched once per cycle and cached by the store.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from .base import MacroPoint, MacroSeries, NotConfigured, ProviderError
from .http import HttpClient

log = logging.getLogger("bullion.providers.macro")

# The series the macro layer scores, with display labels. Anything added here
# shows up in the macro reasoning string automatically.
SERIES: dict[str, str] = {
    "DFII10": "10Y TIPS real yield",
    "DTWEXBGS": "Broad dollar index",
    "CPIAUCSL": "CPI, all items",
    "T10Y2Y": "10Y-2Y spread",
}

# Series where a *rise* is bearish for gold. Everything downstream reads this
# rather than hardcoding signs, because getting one of them backwards produces a
# system that is confidently wrong in exactly the situations that matter most.
INVERSE_TO_GOLD: frozenset[str] = frozenset({"DFII10", "DTWEXBGS", "T10Y2Y"})


def _parse_day(raw: str) -> date | None:
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


@dataclass
class FredMacro:
    """FRED observations. Works with or without an API key -- see module docstring."""

    api_key: str | None = None
    name: str = "fred"
    json_base: str = "https://api.stlouisfed.org/fred/series/observations"
    csv_base: str = "https://fred.stlouisfed.org/graph/fredgraph.csv"
    client: HttpClient = field(default_factory=lambda: HttpClient("fred", min_interval=0.5))

    def series(self, series_id: str, observations: int = 400) -> MacroSeries:
        label = SERIES.get(series_id, series_id)
        points = (
            self._from_json(series_id, observations)
            if self.api_key
            else self._from_csv(series_id, observations)
        )
        if not points:
            raise ProviderError(f"fred: no observations for {series_id}")
        units = "percent" if series_id.startswith(("DFII", "T10Y", "DGS")) else "index"
        return MacroSeries(
            series_id=series_id,
            label=label,
            points=tuple(points),
            units=units,
            source=self.name,
        )

    def _from_json(self, series_id: str, observations: int) -> list[MacroPoint]:
        payload = self.client.get_json(
            self.json_base,
            {
                "series_id": series_id,
                "api_key": self.api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": observations,
            },
        )
        rows = (payload or {}).get("observations") or []
        out: list[MacroPoint] = []
        for row in rows:
            day = _parse_day(row.get("date", ""))
            raw = row.get("value", ".")
            # FRED writes "." for a missing observation (holidays, mostly).
            if day is None or raw in (".", "", None):
                continue
            try:
                out.append(MacroPoint(day=day, value=float(raw)))
            except ValueError:
                continue
        out.sort(key=lambda p: p.day)
        return out[-observations:]

    def _from_csv(self, series_id: str, observations: int) -> list[MacroPoint]:
        rows = self.client.get_csv(self.csv_base, {"id": series_id})
        out: list[MacroPoint] = []
        for row in rows:
            # The CSV's columns are DATE (or observation_date) and the series id.
            day_raw = row.get("DATE") or row.get("observation_date") or ""
            day = _parse_day(day_raw)
            raw = row.get(series_id) or row.get(series_id.upper()) or "."
            if day is None or raw in (".", "", None):
                continue
            try:
                out.append(MacroPoint(day=day, value=float(raw)))
            except ValueError:
                continue
        out.sort(key=lambda p: p.day)
        return out[-observations:]


@dataclass
class PriceFeedMacro:
    """A macro series derived from the price feed, for symbols FRED lacks intraday.

    Twelve Data carries DXY where FRED's broad dollar index updates weekly, and for
    a signal recomputed through the day the weekly series is a lagging indicator
    of a lagging indicator. Where both exist, the price feed wins on freshness and
    FRED wins on authority; the macro scorer is given whichever arrives.
    """

    prices: object
    symbol_map: dict[str, str] = field(
        default_factory=lambda: {"DTWEXBGS": "DXY", "DGS10": "US10Y"}
    )
    name: str = "pricefeed-macro"

    def series(self, series_id: str, observations: int = 400) -> MacroSeries:
        symbol = self.symbol_map.get(series_id)
        if symbol is None:
            raise NotConfigured(f"{self.name} does not carry {series_id}")
        getter = getattr(self.prices, "macro_close_series", None)
        if getter is None:
            raise NotConfigured(f"{type(self.prices).__name__} cannot fetch {symbol}")
        series = getter(symbol, observations)
        points = [MacroPoint(day=c.ts.date(), value=c.close) for c in series]
        return MacroSeries(
            series_id=series_id,
            label=f"{SERIES.get(series_id, series_id)} ({symbol})",
            points=tuple(points),
            units="index",
            source=self.name,
        )
