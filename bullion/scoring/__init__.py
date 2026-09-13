"""The four scoring layers, each normalising to -1..+1 with a reason string.

Imported as modules rather than flattened into functions here, because the layers
share nothing but the return type and keeping them separate is what makes it possible
to re-fit or replace one without touching the others.
"""

from __future__ import annotations

from . import macro, positioning, sentiment, technical
from .layer import LayerScore, unavailable
from .llm_news import (
    CachingScorer,
    ClaudeNewsScorer,
    LexiconScorer,
    NewsScore,
    NewsScorer,
    build_scorer,
)

__all__ = [
    "CachingScorer",
    "ClaudeNewsScorer",
    "LayerScore",
    "LexiconScorer",
    "NewsScore",
    "NewsScorer",
    "build_scorer",
    "macro",
    "positioning",
    "sentiment",
    "technical",
    "unavailable",
]
