"""The one shape every scoring layer returns.

A layer's job is to produce three things, and the third is not optional: a score,
a freshness-discounted measure of how much that score should count, and a sentence
a human can argue with. Spec section 1 makes explainability the product's
differentiator, which means the reason string is part of the return type rather
than a logging nicety -- if a layer cannot say why, it has no business voting.

``available=False`` is distinct from ``score=0.0``. A dark feed is not a neutral
opinion; see ``config.LayerWeights.normalised``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..series import clamp


@dataclass(frozen=True)
class LayerScore:
    """One layer's vote.

    ``score``
        -1 (strong sell) to +1 (strong buy), as spec section 4 defines it.
    ``freshness``
        0..1, how current the underlying data is. Multiplied into the confidence
        calculation, never into the score: stale data does not become *neutral*, it
        becomes *less trustworthy*, and flattening it toward zero would hide that.
    ``components``
        The sub-scores, so the UI can expand a layer and so a backtest can tell
        which part of a layer carried it.
    ``reasoning``
        One line, concrete, with the numbers in it.
    """

    name: str
    score: float
    reasoning: str
    components: dict[str, float] = field(default_factory=dict)
    freshness: float = 1.0
    available: bool = True
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "score", clamp(float(self.score)))
        object.__setattr__(self, "freshness", clamp(float(self.freshness), 0.0, 1.0))

    @property
    def signed_label(self) -> str:
        """``+0.62`` / ``-0.41`` -- the prefix the spec's reasoning strings use."""
        return f"{self.score:+.2f}"

    def as_text(self) -> str:
        return f"{self.signed_label} — {self.reasoning}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": round(self.score, 4),
            "reasoning": self.reasoning,
            "components": {k: round(v, 4) for k, v in self.components.items()},
            "freshness": round(self.freshness, 4),
            "available": self.available,
            "detail": self.detail,
        }


def unavailable(name: str, why: str) -> LayerScore:
    """A layer with no data. Weight redistributes; confidence takes the hit."""
    return LayerScore(
        name=name,
        score=0.0,
        reasoning=f"no data — {why}",
        freshness=0.0,
        available=False,
    )


def weighted(parts: dict[str, tuple[float, float]]) -> float:
    """Weighted mean of ``{name: (score, weight)}``, renormalised over what is present."""
    total = sum(weight for _, weight in parts.values())
    if total <= 0:
        return 0.0
    return clamp(sum(score * weight for score, weight in parts.values()) / total)


def norm(value: float, scale: float) -> float:
    """Map a raw quantity onto -1..+1, where ``scale`` is the "full marks" magnitude.

    Used everywhere a layer turns a price difference or a z-score into a vote. The
    scale constants are the actual tuning surface of this system, so they are passed
    in at each call site with a comment rather than hidden in a helper.
    """
    if scale <= 0:
        return 0.0
    return clamp(value / scale)
