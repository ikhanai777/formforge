"""News ingestion: Finnhub primary, Marketaux fallback, keyword-filtered.

Spec section 3.4 picks these two and warns that off-the-shelf sentiment is too
generic for commodities. That warning is the important part, and it is handled in
two stages:

1. **Filtering happens here**, on keywords, because a general news feed is 95%
   irrelevant to metals and paying an LLM to read it is a waste. The keyword list
   is deliberately wider than "gold" -- the stories that move bullion are about
   real yields, the dollar, the Fed and war, and half of them never name a metal.
2. **Scoring happens in ``scoring/llm_news.py``**, where a Haiku pass re-reads each
   surviving headline for gold/silver *direction* rather than tone. "Gold plunges
   as dollar surges" is negative for the metal and reads as dramatic-neutral to a
   generic classifier; "Fed holds, signals two cuts" names no metal at all and is
   strongly positive.

The provider-supplied sentiment is kept alongside, never replaced, so the two can
be compared later -- which is also how you find out whether the LLM pass is worth
its cost.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..config import GOLD, SILVER
from .base import Headline, NotConfigured, ProviderError
from .http import HttpClient

log = logging.getLogger("bullion.providers.news")

# Lowercased substrings. A headline matching any of these is worth scoring.
KEYWORDS: tuple[str, ...] = (
    "gold",
    "silver",
    "bullion",
    "xau",
    "xag",
    "precious metal",
    "comex",
    "real yield",
    "tips yield",
    "treasury yield",
    "dollar index",
    "dxy",
    "federal reserve",
    "fed ",
    "fomc",
    "powell",
    "rate cut",
    "rate hike",
    "inflation",
    "cpi",
    "central bank",
    "safe haven",
    "geopolitic",
    "sanction",
    "war",
    "etf flows",
)

# Terms that look like a match but are a different story entirely. Without these
# the feed fills up with "Golden State", "Silver Lake" and "Fed Cup".
EXCLUSIONS: tuple[str, ...] = (
    "golden state",
    "silver lake",
    "goldman",
    "gold cup",
    "silver screen",
    "golden globe",
)


def relevant(title: str, summary: str | None = None) -> bool:
    """Keyword gate. Cheap, conservative, and the only filter before the LLM pass."""
    text = f"{title} {summary or ''}".lower()
    if any(bad in text for bad in EXCLUSIONS):
        # Still allow it if it independently names a metal market term.
        if not any(key in text for key in ("bullion", "xau", "xag", "comex", "precious metal")):
            return False
    return any(key in text for key in KEYWORDS)


def assets_mentioned(title: str, summary: str | None = None) -> tuple[str, ...]:
    """Which metals a story names explicitly; empty means macro-wide, not irrelevant.

    An empty tuple is scored against *both* metals, which is correct: a Fed
    headline moves gold and silver without naming either.
    """
    text = f"{title} {summary or ''}".lower()
    found = []
    if any(k in text for k in ("gold", "xau", "bullion")):
        found.append(GOLD)
    if any(k in text for k in ("silver", "xag")):
        found.append(SILVER)
    return tuple(found)


def _stable_id(provider: str, title: str, ts: datetime) -> str:
    """A deduplication key that survives a provider re-serving the same story.

    Wire stories arrive two or three times with different ids and slightly
    different timestamps, and each duplicate would otherwise get its own vote in
    the sentiment average -- quietly weighting whichever story got syndicated
    most.
    """
    digest = hashlib.sha256(f"{provider}|{title.strip().lower()}".encode()).hexdigest()
    return f"{provider[:4]}-{digest[:20]}"


@dataclass
class FinnhubNews:
    api_key: str
    name: str = "finnhub"
    base: str = "https://finnhub.io/api/v1"
    client: HttpClient = field(default_factory=lambda: HttpClient("finnhub", min_interval=1.0))

    def __post_init__(self) -> None:
        if not self.api_key:
            raise NotConfigured("FINNHUB_KEY is not set")

    def headlines(self, since: datetime, limit: int = 50) -> list[Headline]:
        payload = self.client.get_json(
            f"{self.base}/news", {"category": "general", "token": self.api_key}
        )
        if not isinstance(payload, list):
            raise ProviderError(f"finnhub: unexpected payload {str(payload)[:200]}")
        out: list[Headline] = []
        for row in payload:
            title = (row.get("headline") or "").strip()
            summary = (row.get("summary") or "").strip() or None
            if not title or not relevant(title, summary):
                continue
            ts_raw = row.get("datetime")
            if not ts_raw:
                continue
            ts = datetime.fromtimestamp(float(ts_raw), tz=timezone.utc)
            if ts < since:
                continue
            out.append(
                Headline(
                    id=_stable_id(self.name, title, ts),
                    ts=ts,
                    title=title,
                    source=row.get("source") or "finnhub",
                    provider=self.name,
                    url=row.get("url"),
                    summary=summary,
                    # Finnhub's general feed carries no per-article sentiment; the
                    # LLM pass is the only scorer for these.
                    provider_sentiment=None,
                    assets=assets_mentioned(title, summary),
                )
            )
        out.sort(key=lambda h: h.ts)
        return out[-limit:]


@dataclass
class MarketauxNews:
    """Fallback feed, and the one that ships its own sentiment score."""

    api_key: str
    name: str = "marketaux"
    base: str = "https://api.marketaux.com/v1"
    client: HttpClient = field(
        default_factory=lambda: HttpClient("marketaux", min_interval=1.0)
    )

    def __post_init__(self) -> None:
        if not self.api_key:
            raise NotConfigured("MARKETAUX_KEY is not set")

    def headlines(self, since: datetime, limit: int = 50) -> list[Headline]:
        payload = self.client.get_json(
            f"{self.base}/news/all",
            {
                "api_token": self.api_key,
                "search": "gold OR silver OR bullion OR FOMC OR inflation",
                "filter_entities": "true",
                "language": "en",
                "published_after": since.strftime("%Y-%m-%dT%H:%M"),
                "limit": min(limit, 100),
            },
        )
        rows = (payload or {}).get("data") or []
        out: list[Headline] = []
        for row in rows:
            title = (row.get("title") or "").strip()
            summary = (row.get("description") or "").strip() or None
            if not title or not relevant(title, summary):
                continue
            try:
                ts = datetime.fromisoformat(row["published_at"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            out.append(
                Headline(
                    id=_stable_id(self.name, title, ts),
                    ts=ts,
                    title=title,
                    source=row.get("source") or "marketaux",
                    provider=self.name,
                    url=row.get("url"),
                    summary=summary,
                    provider_sentiment=_entity_sentiment(row),
                    assets=assets_mentioned(title, summary),
                )
            )
        out.sort(key=lambda h: h.ts)
        return out[-limit:]


def _entity_sentiment(row: dict) -> float | None:
    """Average Marketaux entity sentiment, already on a -1..+1 scale."""
    scores = [
        float(entity["sentiment_score"])
        for entity in row.get("entities", []) or []
        if entity.get("sentiment_score") is not None
    ]
    if not scores:
        return None
    return max(-1.0, min(1.0, sum(scores) / len(scores)))


@dataclass
class ChainedNews:
    """Try each provider in order; first success wins, failures are reported.

    Not a merge. Two feeds covering the same wire produce near-duplicate headlines
    that survive the id-based dedupe (different wording, same story), and a
    sentiment average over them double-counts. One feed at a time, with a fallback,
    is the honest version.
    """

    providers: tuple[object, ...]
    name: str = "news-chain"

    def headlines(self, since: datetime, limit: int = 50) -> list[Headline]:
        errors: list[str] = []
        for provider in self.providers:
            try:
                found = provider.headlines(since, limit)  # type: ignore[attr-defined]
                if found:
                    return found
                errors.append(f"{getattr(provider, 'name', provider)}: no relevant headlines")
            except ProviderError as exc:
                errors.append(f"{getattr(provider, 'name', provider)}: {exc}")
                log.warning("news provider failed: %s", exc)
        raise ProviderError("; ".join(errors) or "no news providers configured")
