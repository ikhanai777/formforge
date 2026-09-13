"""The sentiment layer: recency-weighted, relevance-weighted, shrunk toward zero.

Three corrections applied to the naive "average the sentiment scores" version, each
of which is the difference between a usable layer and a noise generator:

* **Recency decay.** An 18-hour half-life. Gold reacts to news in minutes and is
  usually done reacting within a session; a two-day-old headline propping up today's
  score is a bug that looks like signal.
* **Relevance as the weight.** A dramatic headline that is not about metals or their
  drivers gets near-zero weight rather than a near-zero direction -- the distinction
  matters because averaging in zeros drags a real reading toward neutral.
* **Shrinkage on thin coverage.** Four headlines cannot support a ±0.9 reading. The
  aggregate is shrunk toward zero by effective sample size, so a quiet news day
  produces a small score rather than a confident one off two stories. This is the
  single most important line in the module: unshrunk, this layer's loudest readings
  come from its thinnest data.

A headline naming only the *other* metal still counts, at half weight: gold and
silver are correlated enough that a silver story is information about gold, and
uncorrelated enough that it is not the same information.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

from ..providers.base import Headline, recency_weight
from ..series import clamp, stdev
from .layer import LayerScore, unavailable
from .llm_news import NewsScore, NewsScorer

HALF_LIFE_HOURS = 18.0

# Effective-sample constant for the shrinkage term. At an effective weight of 3
# (roughly three fresh, fully relevant headlines) the layer reports half of its raw
# reading; by 12 it reports 80%.
SHRINK_K = 3.0

# Headlines older than this are not scored at all -- below the decay floor they only
# add noise and LLM cost.
MAX_AGE_HOURS = 72.0


def _asset_weight(headline: Headline, asset: str) -> float:
    if not headline.assets:
        # Names no metal: a macro story, which moves both.
        return 1.0
    if asset in headline.assets:
        return 1.0
    return 0.5


def _blend(scored: NewsScore, headline: Headline) -> float:
    """Combine our direction with the provider's, if it offered one.

    Weighted toward ours at 3:1. The provider's score is generic-tone sentiment and
    is wrong about metals often enough not to trust it, but it is an independent read
    and discarding it entirely throws away information on the headlines where the two
    agree.
    """
    if headline.provider_sentiment is None:
        return scored.direction
    return clamp(0.75 * scored.direction + 0.25 * headline.provider_sentiment)


def score(
    asset: str,
    headlines: Sequence[Headline],
    scorer: NewsScorer,
    now: datetime | None = None,
) -> LayerScore:
    """Score the sentiment layer for one asset."""
    now = now or datetime.now(timezone.utc)
    fresh_enough = [h for h in headlines if h.age_hours(now) <= MAX_AGE_HOURS]
    if not fresh_enough:
        return unavailable(
            "sentiment",
            f"no relevant headlines in the last {MAX_AGE_HOURS:.0f}h"
            if not headlines
            else f"all {len(headlines)} headlines are older than {MAX_AGE_HOURS:.0f}h",
        )

    scores = scorer.score_batch(fresh_enough)
    if not scores:
        return unavailable("sentiment", "the news scorer returned nothing")

    weighted_sum = 0.0
    weight_total = 0.0
    directions: list[float] = []
    contributions: list[tuple[float, float, Headline, NewsScore]] = []

    for headline in fresh_enough:
        scored = scores.get(headline.id)
        if scored is None:
            continue
        direction = _blend(scored, headline)
        weight = (
            recency_weight(headline.age_hours(now), HALF_LIFE_HOURS)
            * scored.relevance
            * _asset_weight(headline, asset)
        )
        if weight <= 0:
            continue
        weighted_sum += direction * weight
        weight_total += weight
        directions.append(direction)
        contributions.append((weight, direction, headline, scored))

    if weight_total <= 0:
        return unavailable(
            "sentiment",
            f"{len(fresh_enough)} headlines matched keywords but none scored as relevant",
        )

    raw = weighted_sum / weight_total
    # Shrinkage by effective sample size -- see the module docstring.
    shrink = weight_total / (weight_total + SHRINK_K)
    total = clamp(raw * shrink)

    dispersion = stdev(directions)
    # The loudest contributor, named in the reasoning string: a user who disagrees
    # with the sentiment read should be able to see which story caused it.
    contributions.sort(key=lambda row: abs(row[0] * row[1]), reverse=True)
    _, top_direction, top_headline, top_score = contributions[0]

    tone = (
        "net bullish"
        if total > 0.1
        else "net bearish"
        if total < -0.1
        else "no clear lean"
    )
    split = (
        "broad agreement"
        if dispersion < 0.35
        else "sources split"
        if dispersion < 0.6
        else "sources strongly divided"
    )
    source_label = {"llm": "Claude-scored", "lexicon": "keyword-scored"}.get(
        top_score.source, top_score.source
    )
    reasoning = (
        f"{len(contributions)} {source_label} headlines in {MAX_AGE_HOURS:.0f}h, {tone}, "
        f"{split}; loudest: \"{top_headline.title[:90]}\" "
        f"({top_direction:+.2f}, {top_score.rationale or 'no rationale'})"
    )

    return LayerScore(
        name="sentiment",
        score=total,
        reasoning=reasoning,
        components={
            "raw": raw,
            "shrinkage": shrink,
            "dispersion": dispersion,
        },
        # A news layer is fresh if the newest story is recent. The decay already
        # handles the score; this handles how much the score counts.
        freshness=clamp(
            recency_weight(min(h.age_hours(now) for h in fresh_enough), HALF_LIFE_HOURS * 2),
            0.0,
            1.0,
        ),
        detail={
            "headline_count": len(contributions),
            "effective_weight": round(weight_total, 2),
            "scorer": top_score.source,
            "top_headlines": [
                {
                    "title": h.title[:140],
                    "source": h.source,
                    "ts": h.ts.isoformat(),
                    "direction": round(direction, 3),
                    "relevance": round(s.relevance, 3),
                    "rationale": s.rationale,
                    "url": h.url,
                }
                for _, direction, h, s in contributions[:6]
            ],
        },
    )
