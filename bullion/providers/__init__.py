"""Provider assembly: what to use, in what order, and what to do when a key is absent.

``build(settings)`` is the only place in the codebase that decides which feed is
primary. Everything downstream receives a ``Feeds`` bundle and cannot tell whether
it is talking to Twelve Data or to the synthetic generator -- except through
``Feeds.synthetic``, which is surfaced all the way to the UI.

The fallback rule is the one worth stating explicitly: a missing key degrades one
*layer*, never the whole engine. No news key means sentiment is dark and its weight
redistributes to the other three; no COT access means positioning is dark. Only a
missing price feed is fatal, and even then only when synthetic data is refused --
because a signal with no price is not a degraded signal, it is nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings
from .base import (
    CotHistory,
    EconomicEvent,
    Headline,
    MacroSeries,
    NotConfigured,
    ProviderError,
    Quote,
    next_fomc_dates,
)
from .macro import SERIES as MACRO_SERIES
from .macro import FredMacro, PriceFeedMacro
from .news import ChainedNews, FinnhubNews, MarketauxNews
from .positioning import CftcPositioning
from .prices import (
    CrossValidatedPrices,
    GoldApiPrices,
    MetalsApiPrices,
    PriceQuality,
    TwelveDataPrices,
)
from .synthetic import SyntheticCross, SyntheticWorld

log = logging.getLogger("bullion.providers")

__all__ = [
    "CotHistory",
    "CrossValidatedPrices",
    "EconomicEvent",
    "Feeds",
    "Headline",
    "MacroSeries",
    "NotConfigured",
    "PriceQuality",
    "ProviderError",
    "Quote",
    "SyntheticWorld",
    "build",
]


@dataclass
class Feeds:
    """The four ingestion surfaces, any of which may be None except prices."""

    prices: CrossValidatedPrices
    macro: object | None
    news: object | None
    positioning: object | None
    synthetic: bool
    events: tuple[EconomicEvent, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def available_layers(self) -> set[str]:
        """Which fusion layers have a feed at all (not whether it will succeed)."""
        layers = {"technical"}
        if self.macro is not None:
            layers.add("macro")
        if self.news is not None:
            layers.add("sentiment")
        if self.positioning is not None:
            layers.add("positioning")
        return layers

    def describe(self) -> dict[str, str | bool | list[str]]:
        return {
            "prices": self.prices.name,
            "macro": getattr(self.macro, "name", "none"),
            "news": getattr(self.news, "name", "none"),
            "positioning": getattr(self.positioning, "name", "none"),
            "synthetic": self.synthetic,
            "notes": list(self.notes),
        }


def build(settings: Settings, world: SyntheticWorld | None = None) -> Feeds:
    """Assemble feeds from configured credentials, falling back per layer.

    Raises ``NotConfigured`` only for the price layer, and only when synthetic data
    is not permitted -- the production posture, set with ``BULLION_ALLOW_SYNTHETIC=0``,
    where a missing key should page someone rather than quietly serve invented bars.
    """
    notes: list[str] = []
    budget_dir = settings.store_path.parent

    primary: object | None = None
    if settings.twelvedata_key:
        try:
            primary = TwelveDataPrices(api_key=settings.twelvedata_key)
        except NotConfigured as exc:  # pragma: no cover - defensive
            notes.append(str(exc))

    cross: object | None = None
    if settings.goldapi_key:
        try:
            cross = GoldApiPrices(
                api_key=settings.goldapi_key,
                budget_path=Path(budget_dir) / "budget-goldapi.json",
            )
        except NotConfigured as exc:  # pragma: no cover - defensive
            notes.append(str(exc))
    if cross is None and settings.metalsapi_key:
        try:
            cross = MetalsApiPrices(api_key=settings.metalsapi_key)
        except NotConfigured as exc:  # pragma: no cover - defensive
            notes.append(str(exc))

    synthetic = False
    if primary is None:
        if not settings.allow_synthetic:
            raise NotConfigured(
                "no price feed configured. Set TWELVEDATA_API_KEY (primary) and "
                "ideally GOLDAPI_KEY (cross-check), or set BULLION_ALLOW_SYNTHETIC=1 "
                "to run the engine on the labelled synthetic generator."
            )
        world = world or SyntheticWorld()
        primary = world
        cross = cross or SyntheticCross(world)
        synthetic = True
        notes.append(
            "SYNTHETIC DATA: no price key configured, so prices, macro, positioning "
            "and news are generated. Every payload is labelled; do not trade this."
        )
        log.warning(notes[-1])

    prices = CrossValidatedPrices(
        primary=primary, cross=cross if cross is not primary else None
    )

    macro: object | None
    if synthetic:
        macro = world
    else:
        macro = FredMacro(api_key=settings.fred_key)
        if not settings.fred_key:
            notes.append(
                "FRED_API_KEY is not set; using the keyless fredgraph CSV path "
                "(same data, no quota, slightly slower)"
            )
        # Prefer the price feed for the dollar index where it is available: FRED's
        # broad index is weekly and the macro layer recomputes through the day.
        if isinstance(primary, TwelveDataPrices):
            macro = _MacroChain((PriceFeedMacro(prices=primary), macro))

    news: object | None
    if synthetic:
        news = world
    else:
        chain: list[object] = []
        if settings.finnhub_key:
            chain.append(FinnhubNews(api_key=settings.finnhub_key))
        if settings.marketaux_key:
            chain.append(MarketauxNews(api_key=settings.marketaux_key))
        news = ChainedNews(providers=tuple(chain)) if chain else None
        if news is None:
            notes.append(
                "no news key (FINNHUB_KEY or MARKETAUX_KEY); the sentiment layer is "
                "dark and its weight redistributes to the other three"
            )

    positioning: object | None = world if synthetic else CftcPositioning()

    return Feeds(
        prices=prices,
        macro=macro,
        news=news,
        positioning=positioning,
        synthetic=synthetic,
        events=tuple(next_fomc_dates(datetime.now(timezone.utc))),
        notes=tuple(notes),
    )


@dataclass
class _MacroChain:
    """Try each macro source per series; first one that has it wins."""

    sources: tuple[object, ...]
    name: str = "macro-chain"

    def series(self, series_id: str, observations: int = 400) -> MacroSeries:
        errors: list[str] = []
        for source in self.sources:
            try:
                return source.series(series_id, observations)  # type: ignore[attr-defined]
            except (ProviderError, NotConfigured) as exc:
                errors.append(f"{getattr(source, 'name', source)}: {exc}")
        raise ProviderError(f"{series_id}: " + "; ".join(errors))


def macro_series_ids() -> tuple[str, ...]:
    """The macro series the engine scores, in display order."""
    return tuple(MACRO_SERIES)
