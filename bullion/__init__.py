"""Bullion -- confidence-scored, explainable signals for gold and silver.

The product framing is the whole design constraint (spec section 0): nothing in
here claims accuracy. Every call carries a confidence score, the four layer
scores that produced it, and a link to a track record that grades the system's
own past calls against what actually happened. A module that cannot explain its
output does not belong in this package.

Read the layers in this order:

``series``, ``indicators``, ``structure``
    OHLCV containers and the technical maths, computed here rather than taken
    from a provider so the logic is auditable (spec section 3.2).
``providers``
    Price, macro, positioning and news ingestion, every one of them with an
    offline path so the engine runs and is testable without credentials.
``scoring``
    Four independent layers, each normalising to -1..+1 with a human-readable
    reason string. The reason string is not decoration; it is the product.
``regime``, ``fusion``
    Weighting and the composite call. Confidence is computed separately from
    direction, and the engine is allowed to return NO_SIGNAL.
``backtest``, ``scorecard``
    Walk-forward validation and the self-graded record. Not optional extras:
    the scorecard is the trust mechanism the marketing copy is not allowed to
    be (spec sections 5 and 12).
"""

from __future__ import annotations

__version__ = "0.1.0"

from .config import ASSETS, GOLD, SILVER, Regime, Settings, load_settings
from .disclosure import DISCLOSURE, SHORT_DISCLOSURE
from .fusion import SignalState, fuse
from .series import Candle, Series

__all__ = [
    "ASSETS",
    "DISCLOSURE",
    "GOLD",
    "SHORT_DISCLOSURE",
    "SILVER",
    "Candle",
    "Regime",
    "Series",
    "Settings",
    "SignalState",
    "__version__",
    "fuse",
    "load_settings",
]
