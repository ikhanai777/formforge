"""Scoring headlines for *metal direction*, not for tone.

Spec section 3.4's observation is the one that makes this module necessary:
off-the-shelf sentiment is trained on equities and general news, and it gets metals
backwards often enough to be worse than nothing. Two canonical failures:

* "Gold plunges as dollar surges on hot payrolls" -- a generic classifier reads
  dramatic-but-neutral, or even positive on "surges". For gold it is strongly
  negative.
* "Fed holds rates, signals two cuts this year" -- names no metal, so a ticker-keyed
  classifier scores it zero. It is one of the most bullish things that can happen to
  gold.

So each surviving headline is re-scored along two axes that matter here and nowhere
else: **direction for the metal** (-1..+1) and **relevance** (0..1, how much this
story moves bullion at all). Two implementations:

``ClaudeNewsScorer``
    A single batched Haiku call per cycle. Haiku is the right tier for a one-line
    judgement at volume, and batching means one request per recompute rather than
    one per headline -- which is the difference between cents and dollars a day.
``LexiconScorer``
    A keyword fallback for when no key is configured. It is not a model and does not
    pretend to be: it is a floor that keeps the sentiment layer dim rather than dark,
    and it caps its own relevance at 0.6 so the fusion engine never mistakes it for
    the real thing.

The LLM's score never overwrites the provider's. Both are kept (``NewsScore.source``
says which is which) so the pair can be compared later, which is the only way to
find out whether the Haiku pass earns its keep.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..providers.base import Headline
from ..series import clamp

log = logging.getLogger("bullion.scoring.llm_news")


@dataclass(frozen=True)
class NewsScore:
    """One headline's read. ``relevance`` is the weight, ``direction`` is the vote."""

    headline_id: str
    direction: float
    relevance: float
    source: str  # "llm" | "lexicon" | "provider"
    rationale: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "direction", clamp(float(self.direction)))
        object.__setattr__(self, "relevance", clamp(float(self.relevance), 0.0, 1.0))

    @property
    def weighted(self) -> float:
        return self.direction * self.relevance


class NewsScorer(Protocol):
    name: str

    def score_batch(self, headlines: Sequence[Headline]) -> dict[str, NewsScore]: ...


# ---------------------------------------------------------------------------
# Lexicon fallback
# ---------------------------------------------------------------------------

# Phrases and their direction for the metals. Signs are from the transmission
# mechanism, not from how the sentence feels: "hot payrolls" is good news for the
# economy and bad news for gold, because it lifts real yields.
_BULLISH: dict[str, float] = {
    "dovish": 0.7,
    "rate cut": 0.7,
    "cuts rates": 0.8,
    "easing": 0.5,
    "yields fall": 0.7,
    "yields slip": 0.6,
    "real yields lower": 0.8,
    "weaker dollar": 0.7,
    "dollar retreat": 0.6,
    "dollar slides": 0.6,
    "dollar weakens": 0.7,
    "safe haven": 0.6,
    "safe-haven": 0.6,
    "geopolitical": 0.4,
    "tensions escalate": 0.6,
    "escalate": 0.4,
    "central bank buying": 0.6,
    "central banks added": 0.6,
    "reserve diversification": 0.5,
    "inflows": 0.5,
    "record high": 0.3,
    "stimulus": 0.4,
    "cooling inflation": 0.4,
    "cools": 0.3,
    "patience": 0.3,
    "industrial demand": 0.4,
    "supply deficit": 0.6,
    "mine supply falls": 0.5,
}

_BEARISH: dict[str, float] = {
    "hawkish": -0.7,
    "rate hike": -0.7,
    "raises rates": -0.8,
    "higher for longer": -0.6,
    "yields rise": -0.7,
    "yields higher": -0.7,
    "yields jump": -0.7,
    "stronger dollar": -0.7,
    "dollar surges": -0.7,
    "dollar rallies": -0.6,
    "dollar firms": -0.5,
    "outflows": -0.5,
    "profit-taking": -0.3,
    "profit taking": -0.3,
    "risk-on": -0.4,
    "risk appetite returns": -0.4,
    "stronger payrolls": -0.6,
    "hot payrolls": -0.6,
    "beats expectations": -0.3,
    "slides": -0.4,
    "plunges": -0.5,
    "tumbles": -0.5,
    "under pressure": -0.4,
    "selloff": -0.4,
    "ceasefire": -0.4,
    "de-escalat": -0.4,
}

# Terms that make a story relevant to bullion at all, with how much.
_RELEVANCE: dict[str, float] = {
    "gold": 0.9,
    "silver": 0.9,
    "bullion": 0.9,
    "xau": 0.9,
    "xag": 0.9,
    "precious metal": 0.85,
    "comex": 0.7,
    "real yield": 0.8,
    "tips": 0.6,
    "treasury yield": 0.7,
    "fomc": 0.8,
    "federal reserve": 0.75,
    "powell": 0.7,
    "rate cut": 0.7,
    "rate hike": 0.7,
    "inflation": 0.6,
    "cpi": 0.65,
    "dollar index": 0.7,
    "dxy": 0.7,
    "central bank": 0.6,
    "geopolitic": 0.55,
    "sanction": 0.5,
    "war": 0.5,
    "etf": 0.5,
}

# "gold falls" is bearish; the lexicon above scores directional verbs without a
# subject, so a metal subject plus a fall verb is matched explicitly here.
_FALL_VERBS = re.compile(
    r"\b(gold|silver|bullion|metals?)\b[^.]{0,40}?"
    r"\b(fall|falls|fell|drop|drops|slip|slips|slide|slides|sink|sinks|lower|retreat)\b",
    re.IGNORECASE,
)
_RISE_VERBS = re.compile(
    r"\b(gold|silver|bullion|metals?)\b[^.]{0,40}?"
    r"\b(rise|rises|rose|gain|gains|jump|jumps|climb|climbs|rally|rallies|advance|higher)\b",
    re.IGNORECASE,
)


@dataclass
class LexiconScorer:
    """Keyword scoring. A floor, not a model -- see the module docstring."""

    name: str = "lexicon"
    # Relevance ceiling. The fusion engine reads relevance as a weight, so capping
    # it is how the lexicon declares its own limits instead of being trusted like
    # the LLM pass.
    max_relevance: float = 0.6

    def score_batch(self, headlines: Sequence[Headline]) -> dict[str, NewsScore]:
        return {h.id: self.score_one(h) for h in headlines}

    def score_one(self, headline: Headline) -> NewsScore:
        text = f"{headline.title} {headline.summary or ''}".lower()
        hits: list[tuple[str, float]] = []
        for phrase, weight in _BULLISH.items():
            if phrase in text:
                hits.append((phrase, weight))
        for phrase, weight in _BEARISH.items():
            if phrase in text:
                hits.append((phrase, weight))
        if _FALL_VERBS.search(text):
            hits.append(("metal falling", -0.6))
        if _RISE_VERBS.search(text):
            hits.append(("metal rising", 0.6))

        direction = 0.0
        if hits:
            # Mean rather than sum: a headline stacking three bullish phrases is not
            # three times as bullish, it is one bullish headline written with energy.
            direction = sum(weight for _, weight in hits) / len(hits)

        relevance = 0.0
        for term, weight in _RELEVANCE.items():
            if term in text:
                relevance = max(relevance, weight)
        relevance = min(relevance, self.max_relevance)
        if not hits:
            # Relevant but with no directional read: the story matters and we cannot
            # say which way, which is a lower weight rather than a zero direction.
            relevance *= 0.4

        rationale = ", ".join(phrase for phrase, _ in hits[:3]) or "no directional phrases"
        return NewsScore(
            headline_id=headline.id,
            direction=direction,
            relevance=relevance,
            source=self.name,
            rationale=rationale,
        )


# ---------------------------------------------------------------------------
# Claude pass
# ---------------------------------------------------------------------------

PROMPT = """\
You score news headlines for their effect on precious metals prices (gold XAU/USD \
and silver XAG/USD). You are not scoring tone, drama, or whether the news is good \
for the economy. You are scoring the expected direction of the metal.

Use the transmission mechanism, not the mood of the sentence:
- Lower real yields, a weaker dollar, dovish Fed, rate cuts, central bank gold \
buying, geopolitical escalation, ETF inflows => POSITIVE for metals.
- Higher real yields, a stronger dollar, hawkish Fed, strong US growth or labour \
data, de-escalation, ETF outflows => NEGATIVE for metals.
- A headline about gold or silver already having moved is information about flow and \
momentum, and should be scored in the direction of the move, with modest magnitude.
- Silver is about half industrial demand: solar, electronics and manufacturing \
stories matter for silver and barely touch gold.

For each headline return:
  direction: -1.0 (strongly bearish for metals) to +1.0 (strongly bullish)
  relevance: 0.0 (irrelevant to metals) to 1.0 (directly and materially about them)
  rationale: at most 12 words naming the mechanism, not restating the headline

Relevance is the weight your direction gets. A headline that is dramatic but not \
about metals or their drivers should get relevance near 0, whatever its direction.

Return ONLY a JSON array, one object per headline, in the same order, each with \
keys "id", "direction", "relevance", "rationale". No prose, no markdown fence.\
"""


@dataclass
class ClaudeNewsScorer:
    """One batched Haiku call per cycle, with the lexicon as the failure path.

    Every failure mode -- no SDK, bad key, rate limit, malformed JSON, a short array
    -- degrades to the lexicon for the headlines that are missing rather than taking
    the sentiment layer down. A news classifier is not load-bearing enough to fail a
    cycle over.
    """

    api_key: str
    model: str = "claude-haiku-4-5"
    name: str = "llm"
    max_headlines: int = 40
    max_tokens: int = 2048
    fallback: LexiconScorer = field(default_factory=LexiconScorer)
    _client: Any = field(default=None, repr=False)

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "the anthropic SDK is not installed; install bullion[llm] or unset "
                "ANTHROPIC_API_KEY to use the lexicon scorer"
            ) from exc
        self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def score_batch(self, headlines: Sequence[Headline]) -> dict[str, NewsScore]:
        if not headlines:
            return {}
        batch = list(headlines)[-self.max_headlines :]
        try:
            scored = self._call(batch)
        except Exception as exc:
            log.warning("Haiku news pass failed (%s); falling back to the lexicon", exc)
            return self.fallback.score_batch(batch)

        # Fill any gaps from the lexicon rather than dropping the headline, so a
        # truncated response costs precision and not coverage.
        missing = [h for h in batch if h.id not in scored]
        if missing:
            log.info(
                "Haiku returned %d/%d headlines; lexicon filled the rest",
                len(scored),
                len(batch),
            )
            scored.update(self.fallback.score_batch(missing))
        return scored

    def _call(self, batch: list[Headline]) -> dict[str, NewsScore]:
        client = self._ensure_client()
        listing = "\n".join(
            f'{{"id": "{h.id}", "headline": {json.dumps(h.title)}, '
            f'"source": {json.dumps(h.source)}}}'
            for h in batch
        )
        response = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[
                # The instruction block is identical every call, so it caches; the
                # headlines, which change, go in the user turn after it.
                {"type": "text", "text": PROMPT, "cache_control": {"type": "ephemeral"}}
            ],
            messages=[{"role": "user", "content": f"Headlines:\n{listing}"}],
        )
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        return self._parse(text, batch)

    def _parse(self, text: str, batch: list[Headline]) -> dict[str, NewsScore]:
        raw = text.strip()
        # Tolerate a fence even though the prompt forbids one.
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            raw = raw[4:] if raw.lower().startswith("json") else raw
        start, end = raw.find("["), raw.rfind("]")
        if start == -1 or end == -1:
            raise ValueError(f"no JSON array in response: {text[:200]!r}")
        rows = json.loads(raw[start : end + 1])

        by_id = {h.id: h for h in batch}
        out: dict[str, NewsScore] = {}
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            # Positional fallback: the prompt asks for ids, and a model that drops
            # them has still answered in order.
            hid = str(row.get("id") or "")
            if hid not in by_id:
                hid = batch[index].id if index < len(batch) else ""
            if hid not in by_id:
                continue
            try:
                out[hid] = NewsScore(
                    headline_id=hid,
                    direction=float(row.get("direction", 0.0)),
                    relevance=float(row.get("relevance", 0.0)),
                    source=self.name,
                    rationale=str(row.get("rationale", ""))[:120],
                )
            except (TypeError, ValueError):
                continue
        if not out:
            raise ValueError(f"no usable rows in response: {text[:200]!r}")
        return out


@dataclass
class CachingScorer:
    """Memoises by headline id, so a cycle scores each story exactly once.

    Not an optimisation detail -- it is a 4x cost reduction. The same headline batch is
    scored for gold, for silver, and again when the cycle persists the scores, so an
    uncached scorer makes four Haiku calls per recompute where one will do. The cache is
    per-cycle (the scorer is rebuilt each cycle) because a headline's score should be
    allowed to change when the prompt or model does.
    """

    inner: NewsScorer
    name: str = "cached"
    _cache: dict[str, NewsScore] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.name = getattr(self.inner, "name", "cached")

    def score_batch(self, headlines: Sequence[Headline]) -> dict[str, NewsScore]:
        missing = [h for h in headlines if h.id not in self._cache]
        if missing:
            self._cache.update(self.inner.score_batch(missing))
        return {h.id: self._cache[h.id] for h in headlines if h.id in self._cache}


def build_scorer(api_key: str | None, model: str = "claude-haiku-4-5") -> NewsScorer:
    """Haiku when a key exists, the lexicon when it does not -- both cached."""
    inner: NewsScorer = (
        ClaudeNewsScorer(api_key=api_key, model=model) if api_key else LexiconScorer()
    )
    return CachingScorer(inner=inner)
