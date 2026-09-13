"""CFTC Commitment of Traders: weekly large-speculator positioning.

Spec section 3.3 is right that this is the layer most retail apps skip, and the
reason it is worth the parsing work is that it behaves differently from the other
three: it is *confirming in the middle of its range and contrarian at the
extremes*. Specs adding to a net long from a neutral base is fuel. Specs at a
three-year extreme is a crowded trade with nobody left to buy, and gold's sharpest
drawdowns start there.

Two facts about the data that shape the code:

* It is published Friday afternoon for the preceding **Tuesday**. So the freshest
  reading is already three days stale on arrival and up to ten days stale before
  the next one lands. The scorer is told the age and discounts accordingly rather
  than treating Monday's figure as current.
* The field names differ between the legacy and disaggregated reports and between
  the CSV and the Socrata JSON. Rather than pin one spelling, every field is
  resolved against a list of candidates -- a column rename upstream should degrade
  to a clear error, not to zeros that silently neutralise the layer.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ..config import GOLD, SILVER
from .base import CotHistory, CotReport, ProviderError
from .http import HttpClient

log = logging.getLogger("bullion.providers.positioning")

# Contract names as they appear in the reports. COMEX gold and silver, futures
# only, legacy report.
MARKET_NAMES: dict[str, tuple[str, ...]] = {
    GOLD: ("GOLD - COMMODITY EXCHANGE INC.",),
    SILVER: ("SILVER - COMMODITY EXCHANGE INC.",),
}

_FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "market": (
        "market_and_exchange_names",
        "Market_and_Exchange_Names",
        "market_and_exchange_name",
    ),
    "report_date": (
        "report_date_as_yyyy_mm_dd",
        "Report_Date_as_YYYY-MM-DD",
        "report_date_as_yyyy_mm_dd_",
        "yyyy_report_week_ww",
    ),
    "long": (
        "noncomm_positions_long_all",
        "NonComm_Positions_Long_All",
        "noncommercial_long",
    ),
    "short": (
        "noncomm_positions_short_all",
        "NonComm_Positions_Short_All",
        "noncommercial_short",
    ),
    "oi": ("open_interest_all", "Open_Interest_All", "open_interest"),
}


def _field(row: dict[str, Any], key: str) -> Any:
    for candidate in _FIELD_CANDIDATES[key]:
        if candidate in row:
            return row[candidate]
    # Case-insensitive second pass: Socrata and the CSV disagree about case.
    lowered = {k.lower().replace("-", "_"): v for k, v in row.items()}
    for candidate in _FIELD_CANDIDATES[key]:
        hit = lowered.get(candidate.lower().replace("-", "_"))
        if hit is not None:
            return hit
    raise ProviderError(
        f"CFTC row is missing {key!r}; tried {_FIELD_CANDIDATES[key]}, "
        f"row has {sorted(row)[:8]}"
    )


def _as_float(value: Any) -> float:
    if value in (None, "", "."):
        raise ProviderError("CFTC row has an empty numeric field")
    return float(str(value).replace(",", ""))


def _as_date(value: Any) -> date:
    text = str(value)[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ProviderError(f"unparseable CFTC report date {value!r}")


def parse_rows(asset: str, rows: Iterable[dict[str, Any]]) -> CotHistory:
    """Turn CFTC rows (CSV or JSON, any spelling) into a history, oldest first.

    Exposed separately from the fetch so the parser is testable against a fixture
    and so a user who has downloaded the annual archive by hand can load it.
    """
    wanted = {name.upper() for name in MARKET_NAMES.get(asset, ())}
    reports: dict[date, CotReport] = {}
    for row in rows:
        try:
            market = str(_field(row, "market")).strip().upper()
        except ProviderError:
            continue
        if wanted and market not in wanted:
            continue
        try:
            report = CotReport(
                asset=asset,
                report_date=_as_date(_field(row, "report_date")),
                noncomm_long=_as_float(_field(row, "long")),
                noncomm_short=_as_float(_field(row, "short")),
                open_interest=_as_float(_field(row, "oi")),
            )
        except ProviderError as exc:
            log.debug("skipping CFTC row: %s", exc)
            continue
        reports[report.report_date] = report
    if not reports:
        raise ProviderError(
            f"no CFTC rows matched {asset} (looked for {sorted(wanted)}); "
            "the contract name or the report format may have changed"
        )
    return CotHistory(asset=asset, reports=tuple(reports[d] for d in sorted(reports)))


@dataclass
class CftcPositioning:
    """Socrata JSON endpoint for the legacy futures-only COT report.

    No key and no quota, so no budget wrapper -- but it is one request per asset per
    week of genuinely new data, and the store caches it, so the cycle does not
    re-download three years of history every five minutes.
    """

    name: str = "cftc"
    base: str = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
    client: HttpClient = field(default_factory=lambda: HttpClient("cftc", min_interval=1.0))

    def history(self, asset: str, weeks: int = 156) -> CotHistory:
        names = MARKET_NAMES.get(asset)
        if not names:
            raise ProviderError(f"no COT contract mapping for {asset}")
        # Socrata's SoQL: filter server-side so three years is ~150 rows, not 1.5M.
        clause = " or ".join(
            f"market_and_exchange_names='{name}'" for name in names
        )
        rows = self.client.get_json(
            self.base,
            {
                "$where": clause,
                "$order": "report_date_as_yyyy_mm_dd DESC",
                "$limit": weeks,
            },
        )
        if not isinstance(rows, list):
            raise ProviderError(f"cftc: unexpected payload {str(rows)[:200]}")
        return parse_rows(asset, rows)
