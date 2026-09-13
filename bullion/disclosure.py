"""The compliance disclosure, in one place because it has to appear everywhere.

Spec section 11. An unlicensed product that frames its output as investment
advice is a regulatory problem in most markets (SEC/CFTC, FCA, ASIC all require
registration for personalised advice), and the two cheap mitigations are
vocabulary and disclosure. Both are enforced here rather than left to whoever
writes the next template:

* **Vocabulary.** ``FORBIDDEN_PHRASINGS`` is checked by a test. The engine emits
  "signal", "score" and "confidence"; it never emits "recommendation", "advice",
  "guaranteed" or "profit". That is not pedantry -- the word choice is the thing
  a regulator reads first.
* **Disclosure.** Every signal payload carries ``SHORT_DISCLOSURE`` and every
  API response carries it in a ``disclosure`` field, so a client cannot render a
  call without having been handed the caveat. The dashboard shows the long form
  persistently, not behind a dismissable banner.

None of this is a substitute for a lawyer before public launch.
"""

from __future__ import annotations

DISCLOSURE = (
    "Bullion publishes probabilistic, model-generated signals for informational "
    "and educational purposes only. It is not investment advice, not a "
    "recommendation to buy or sell any instrument, and not personalised to your "
    "circumstances, objectives or risk tolerance. No signal is a forecast of "
    "future prices. Gold and silver are driven by real yields, central bank "
    "policy, the dollar and geopolitical shocks, and any of those can invalidate "
    "a signal within minutes of it being issued. Past performance of these "
    "signals -- including the track record shown in this app -- does not predict "
    "future performance. Trading leveraged precious metals can lose you more "
    "than your deposit. Bullion is not a registered investment adviser or "
    "broker. Do your own research and consider taking licensed advice."
)

SHORT_DISCLOSURE = (
    "Informational only, not financial advice. Signals are probabilistic and "
    "can be wrong."
)

# Language the product is not allowed to use about itself, checked in tests.
# The left-hand side is the claim; the right-hand side is what to say instead.
FORBIDDEN_PHRASINGS: dict[str, str] = {
    "guaranteed": "say 'confidence score' and publish the win rate",
    "guarantee": "say 'confidence score' and publish the win rate",
    "highly accurate": "publish the measured win rate per strength bucket",
    "risk-free": "nothing here is risk-free; drop the claim",
    "you should buy": "the signal state is BUY; the user decides",
    "you should sell": "the signal state is SELL; the user decides",
    "we recommend": "say 'the signal reads' -- a recommendation is advice",
    "financial advice": "only ever in the negative, as in 'not financial advice'",
}


def annotate(payload: dict) -> dict:
    """Attach the short disclosure to an outbound payload.

    Called on every API response rather than documented as a convention, because
    a convention is something the next endpoint forgets.
    """
    out = dict(payload)
    out["disclosure"] = SHORT_DISCLOSURE
    return out
