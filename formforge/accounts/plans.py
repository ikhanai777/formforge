"""The plans, in one place (`docs/monetization-site-spec.md` section 2).

There is exactly one reason this is a module rather than three constants
scattered across the paywall, the checkout page and the renewal job: a price
that disagrees with itself is not a display bug, it is a customer who was
charged one number and shown another. Everything that needs to know what a
tier costs or grants reads it from here.

The numbers themselves are *placeholders pending measurement*, and the spec
says so. `price_usd` and `credits` are a guess about what a maker will pay;
the cost side is knowable and is not yet known, because it comes from the
sandbox's own CPU and wall-clock measurements per generator, which Phase 2 is
what wires to a dashboard. Nothing here should be read as a validated price.
"""

from __future__ import annotations

from dataclasses import dataclass

# What one credit buys: one successful, validated generation that produced a
# downloadable bundle. A build that failed its preconditions or its DFM checks
# is not a credit -- see AccountStore.spend, which is only ever called after
# validation passes.
CREDIT = "one validated generation"

# The price of a credit bought outside a plan, and the price a plan's overage
# falls through to rather than stopping dead. A hard stop mid-batch is the
# worst possible moment to lose a paying customer.
PAY_AS_YOU_GO_USD = 0.30


@dataclass(frozen=True, slots=True)
class Plan:
    """One tier. Frozen: a plan that can be mutated at runtime is a pricing
    incident waiting for a race condition."""

    id: str
    name: str
    price_usd: float
    credits: int
    formats: tuple[str, ...]
    batch: bool
    priority_queue: bool

    @property
    def paid(self) -> bool:
        return self.price_usd > 0


# STL only on free, everything on paid: the free tier has to prove the loop
# works without being the product. STEP is the line that matters to the seller
# persona -- it is the format you can actually edit downstream.
FREE_FORMATS = ("stl",)
PAID_FORMATS = ("stl", "3mf", "step", "source")

PLANS: dict[str, Plan] = {
    "free": Plan(
        id="free",
        name="Free",
        price_usd=0.0,
        # Three is a guess, and is flagged as open question 3 in the spec. It
        # moves on conversion data from Phase 2's dashboards, not on a second
        # guess.
        credits=3,
        formats=FREE_FORMATS,
        batch=False,
        priority_queue=False,
    ),
    "maker": Plan(
        id="maker",
        name="Maker",
        price_usd=9.0,
        credits=60,
        formats=PAID_FORMATS,
        batch=False,
        priority_queue=False,
    ),
    "studio": Plan(
        id="studio",
        name="Studio",
        price_usd=29.0,
        credits=300,
        formats=PAID_FORMATS,
        batch=True,
        priority_queue=True,
    ),
}

DEFAULT_PLAN = "free"


def get(plan_id: str) -> Plan:
    """The plan, or a KeyError naming what was asked for.

    Deliberately not falling back to free on an unknown id. A typo in a plan
    name silently downgrading a paying customer is far worse than a loud
    failure at the one place that can still be fixed before they notice.
    """
    try:
        return PLANS[plan_id]
    except KeyError:
        raise KeyError(
            f"unknown plan {plan_id!r}; known plans are {sorted(PLANS)}"
        ) from None


def allows_format(plan_id: str, fmt: str) -> bool:
    return fmt in get(plan_id).formats
