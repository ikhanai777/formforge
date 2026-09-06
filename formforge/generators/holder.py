"""The candle holder definition: a style and a seed in, a parameter set out.

The fourth generator on the same solver, and the first whose subject is not a
shape but an *object with a job*. A vase holds whatever you put in it, so every
number in `vase.py` and `sculpt.py` scales with the height. A candle holder
holds a candle, and a tealight is 39 mm across whatever size you make the
holder -- so the interesting rule in this file is the one that does *not*
scale. The socket keeps its diameter; the body grows around it; and when the
body cannot grow around it any more, this is the node that says so and widens
the body rather than shrinking the hole, because a tealight holder a tealight
does not fit in is not a smaller holder, it is a failed one.

    style -------> preset ------\\
    seed --------> draws --------> jittered --> proportioned --> feasible --> params
    variation ------------------/

The other domain rule is the plan. A star or a scallop takes material out
between its arms, and the socket has to fit inside the *valley*, not inside the
circle the arms reach to. `plan_low` below is that valley, computed the same way
the template computes it, and it is what turns "a snowflake tealight" into a
width the geometry can actually carry.
"""

from __future__ import annotations

import math
from typing import Any

from .graph import Definition, Solution
from .mushroom import unit

TEMPLATE_ID = "decor_candle_holder"

# Every style is a set of slider positions, written as they are meant to be
# seen at the style's own width. The proportion node rescales what should grow
# with the body and leaves the socket alone.
STYLES: dict[str, dict[str, Any]] = {
    "heart": {
        "width_mm": 128, "height_mm": 52, "base_frac": 0.68, "top_frac": 0.72,
        "belly_pos": 0.42, "shoulder": 0.75, "plan": "heart", "plan_depth": 0.42,
        "rib_count": 30, "rib_mm": 2.2, "rib_width": 0.45, "rib_sharp": 0.85,
        "socket_d_mm": 40.5, "socket_depth_mm": 17, "wall_mm": 3.2, "floor_mm": 4,
    },
    "bubble": {
        "width_mm": 96, "height_mm": 34, "base_frac": 0.68, "top_frac": 0.78,
        "belly_pos": 0.5, "shoulder": 0.9, "plan": "petal", "plan_points": 9,
        "plan_depth": 0.16, "rib_count": 18, "rib_mm": 2.6, "rib_width": 0.9,
        "rib_sharp": 0.15, "rib_rows": 3, "socket_d_mm": 40.5,
        "socket_depth_mm": 14, "wall_mm": 3.4, "floor_mm": 4,
    },
    "flake": {
        "width_mm": 138, "height_mm": 22, "base_frac": 0.98, "top_frac": 0.94,
        "belly_pos": 0.6, "shoulder": 0.3, "plan": "star", "plan_points": 6,
        "plan_depth": 0.42, "rib_count": 0, "socket_d_mm": 40.5,
        "socket_depth_mm": 13, "wall_mm": 3.5, "floor_mm": 5,
    },
    "moon": {
        "width_mm": 110, "height_mm": 22, "base_frac": 0.96, "top_frac": 0.9,
        "belly_pos": 0.5, "shoulder": 0.4, "plan": "round", "rib_count": 0,
        "socket_d_mm": 42, "socket_depth_mm": 15, "back": "crescent",
        "back_h_mm": 132, "back_t_mm": 9, "wall_mm": 4, "floor_mm": 5,
    },
    "lantern": {
        "width_mm": 82, "height_mm": 108, "base_frac": 1.0, "top_frac": 1.0,
        "belly_pos": 0.5, "shoulder": 0.1, "plan": "polygon", "plan_points": 4,
        "plan_depth": 0.55, "rib_count": 0, "pierce_count": 8, "pierce_w": 0.42,
        "pierce_lo": 0.16, "pierce_hi": 0.86, "socket_d_mm": 52,
        "socket_depth_mm": 100, "wall_mm": 3, "floor_mm": 4,
    },
    "flute": {
        "width_mm": 88, "height_mm": 44, "base_frac": 0.66, "top_frac": 0.82,
        "belly_pos": 0.4, "shoulder": 0.7, "plan": "round",
        "rib_count": 34, "rib_mm": 2.0, "rib_width": 0.4, "rib_sharp": 1.0,
        "socket_d_mm": 40.5, "socket_depth_mm": 16, "wall_mm": 3, "floor_mm": 4,
    },
    "bloom": {
        "width_mm": 112, "height_mm": 30, "base_frac": 0.72, "top_frac": 0.84,
        "belly_pos": 0.62, "shoulder": 0.85, "plan": "petal", "plan_points": 8,
        "plan_depth": 0.3, "rib_count": 24, "rib_mm": 1.6, "rib_width": 0.55,
        "rib_sharp": 0.5, "socket_d_mm": 40.5, "socket_depth_mm": 13,
        "wall_mm": 3.2, "floor_mm": 4,
    },
    "pillar": {
        "width_mm": 92, "height_mm": 96, "base_frac": 0.78, "top_frac": 0.7,
        "belly_pos": 0.3, "shoulder": 0.65, "plan": "round", "rib_count": 30,
        "rib_mm": 2.4, "rib_width": 0.5, "rib_sharp": 0.7,
        "socket_d_mm": 52, "socket_depth_mm": 60, "wall_mm": 3.4, "floor_mm": 5,
    },
    "tower": {
        "width_mm": 84, "height_mm": 118, "base_frac": 1.0, "top_frac": 0.94,
        "belly_pos": 0.5, "shoulder": 0.2, "plan": "polygon", "plan_points": 6,
        "plan_depth": 0.55, "rib_count": 0, "pierce_count": 12, "pierce_w": 0.36,
        "pierce_lo": 0.2, "pierce_hi": 0.9, "socket_d_mm": 48,
        "socket_depth_mm": 108, "wall_mm": 3, "floor_mm": 4,
    },
    "arcade": {
        "width_mm": 104, "height_mm": 26, "base_frac": 0.94, "top_frac": 0.86,
        "belly_pos": 0.5, "shoulder": 0.4, "plan": "round", "rib_count": 36,
        "rib_mm": 1.8, "rib_width": 0.45, "rib_sharp": 0.9,
        "socket_d_mm": 40.5, "socket_depth_mm": 17, "back": "arch",
        "back_h_mm": 118, "back_t_mm": 8, "wall_mm": 3.4, "floor_mm": 5,
    },
    "plaque": {
        "width_mm": 96, "height_mm": 24, "base_frac": 0.95, "top_frac": 0.88,
        "belly_pos": 0.5, "shoulder": 0.3, "plan": "polygon", "plan_points": 8,
        "plan_depth": 0.5, "rib_count": 0, "socket_d_mm": 40.5,
        "socket_depth_mm": 15, "back": "tablet", "back_h_mm": 96,
        "back_t_mm": 8, "wall_mm": 3.6, "floor_mm": 5,
    },
    "reef": {
        "width_mm": 92, "height_mm": 46, "base_frac": 0.55, "top_frac": 0.74,
        "belly_pos": 0.55, "shoulder": 1.0, "plan": "round", "rib_count": 20,
        "rib_mm": 2.6, "rib_width": 0.85, "rib_sharp": 0.1, "rib_rows": 4,
        "rib_vary": 0.55, "socket_d_mm": 40.5, "socket_depth_mm": 16,
        "wall_mm": 3.4, "floor_mm": 4,
    },
}

STYLE_NOTE = {
    "heart": "a ribbed heart, squat, for one tealight",
    "bubble": "scalloped in plan and blown into rows of bubbles",
    "flake": "a flat six-armed star with the cup sunk into the middle",
    "moon": "a low dish with a crescent moon standing behind the flame",
    "lantern": "a square pierced box -- the flame shows through the windows",
    "flute": "the plain round one, finely fluted the whole way up",
    "bloom": "eight broad petals with a light reeding over them",
    "pillar": "tall and ribbed, bored deep for a pillar candle",
    "tower": "a hexagonal pierced tower",
    "arcade": "a reeded dish under an open arch",
    "plaque": "an octagonal dish with a plain tablet behind it",
    "reef": "rows of uneven bubbles on a lifted, rounded body",
}

# How far each slider may wander from its style. Tighter than the vases' on
# anything the candle touches, and loose on the decoration.
JITTER: dict[str, tuple[str, float]] = {
    "width_mm": ("rel", 0.12),
    "height_mm": ("rel", 0.16),
    "base_frac": ("abs", 0.06),
    "top_frac": ("abs", 0.06),
    "belly_pos": ("abs", 0.07),
    "shoulder": ("abs", 0.18),
    "oval": ("abs", 0.06),
    "plan_depth": ("abs", 0.08),
    "rib_mm": ("rel", 0.2),
    "rib_width": ("abs", 0.08),
    "rib_sharp": ("abs", 0.16),
    "rib_vary": ("abs", 0.2),
    "pierce_w": ("abs", 0.06),
    "pierce_lo": ("abs", 0.04),
    "pierce_hi": ("abs", 0.04),
    "socket_depth_mm": ("rel", 0.12),
    "back_t_mm": ("abs", 1.0),
    "wall_mm": ("abs", 0.2),
    "floor_mm": ("abs", 0.6),
}

# What grows when the body grows: the height of a standing back, and how many
# ribs fit round it at the same spacing. Not the socket -- see the module
# docstring. Not the wall either: a printed wall is a number of beads, and a
# bigger holder does not want thicker beads.
PROPORTIONAL = ("back_h_mm",)

DEFINITION = Definition("holder")

DEFINITION.slider(
    "style", "heart", choices=(*STYLES, "mixed"),
    doc="Which body, plan and decoration to start from; `mixed` picks one per seed.",
)
DEFINITION.slider(
    "seed", 1, low=0, high=9999, doc="Drives the jitter and the choice under `mixed`."
)
DEFINITION.slider(
    "variation", 0.55, low=0.0, high=1.0,
    doc="How far a holder may wander from its style. 0 rebuilds the style exactly.",
)
DEFINITION.slider(
    "overrides", {},
    doc="Slider values pinned by the caller; honoured unless the geometry cannot take them.",
)


def plan_low(plan: str, depth: float, points: int) -> float:
    """The smallest radius of a plan whose largest is 1.

    The template normalises every outline to peak at 1 and then blends it toward
    a circle by `depth`, so this is the valley between the arms -- and the
    valley, not the width, is what the socket has to fit inside.
    """
    depth = min(max(depth, 0.0), 0.9)
    sides = max(3, int(points))
    if plan == "round" or depth <= 0.0:
        return 1.0
    if plan == "polygon":
        low = math.cos(math.pi / sides)
    elif plan == "star":
        low = 0.0
    elif plan == "petal":
        low = 0.0
    else:  # heart: the cusp at the back runs all the way in
        low = 0.0
    return 1.0 - depth + depth * low


@DEFINITION.component(name="bounds")
def _bounds() -> dict[str, dict[str, Any]]:
    """The template schema: the authority on what the geometry accepts."""
    return schema_bounds()


@DEFINITION.component("style", "seed", "bounds", "overrides", name="preset")
def _preset(
    style: str, seed: int, bounds: dict[str, dict[str, Any]], overrides: dict[str, Any]
) -> dict[str, Any]:
    """The value list: slider positions for one style, over the defaults."""
    if style == "mixed":
        names = tuple(STYLES)
        style = names[int(unit(seed, "style") * len(names)) % len(names)]
    base = {name: spec.get("default") for name, spec in bounds.items()}
    base.update(STYLES[style])
    base.update({k: v for k, v in (overrides or {}).items() if k in bounds})
    base["style"] = style
    return base


@DEFINITION.component("seed", name="draws")
def _draws(seed: int) -> dict[str, float]:
    """One stable unit draw per parameter name."""
    keys = (*JITTER, *PROPORTIONAL, "rib_count", "style")
    return {key: unit(seed, "holder:" + key) for key in keys}


@DEFINITION.component("preset", "draws", "variation", "overrides", name="jittered")
def _jittered(
    preset: dict[str, Any],
    draws: dict[str, float],
    variation: float,
    overrides: dict[str, Any],
) -> dict[str, Any]:
    """Move the free sliders off the style, by up to their own jitter range."""
    out = dict(preset)
    pinned = set(overrides or ())
    for name, (mode, amount) in JITTER.items():
        value = preset.get(name)
        if name in pinned or not isinstance(value, (int, float)):
            continue
        swing = (draws[name] * 2.0 - 1.0) * variation
        out[name] = value * (1.0 + amount * swing) if mode == "rel" else value + amount * swing
    return out


@DEFINITION.component(
    "preset", "jittered", "draws", "variation", "overrides", name="proportioned"
)
def _proportioned(
    preset: dict[str, Any],
    jittered: dict[str, Any],
    draws: dict[str, float],
    variation: float,
    overrides: dict[str, Any],
) -> dict[str, Any]:
    """Rebuild what belongs to the body's size, and count the ribs.

    A holder that came out 12% wider gets *more ribs*, not wider ones: a rib is
    a printed feature with a width the nozzle cares about, so the thing that
    scales with the size of the body is how many fit round it at the spacing the
    style chose. The socket is deliberately absent from this node.
    """
    out = dict(jittered)
    pinned = set(overrides or ())
    style = STYLES[preset["style"]]
    width = out["width_mm"]
    reference = style.get("width_mm", preset["width_mm"])

    for name in PROPORTIONAL:
        if name in pinned:
            continue
        ratio = style.get(name, preset[name]) / max(reference, 1e-6)
        wobble = 1.0 + 0.06 * variation * (draws[name] * 2.0 - 1.0)
        out[name] = width * ratio * wobble

    count = style.get("rib_count", preset["rib_count"])
    if count >= 3 and "rib_count" not in pinned:
        pitch = reference / max(count, 1)
        out["rib_count"] = max(4, round(width / pitch * (0.92 + 0.16 * draws["rib_count"])))
    return out


@DEFINITION.component("proportioned", name="feasible")
def _feasible(params: dict[str, Any]) -> dict[str, Any]:
    """Satisfy the template's preconditions, and the CPU budget behind them."""
    out = dict(params)

    # The squash comes off everything measured across it, so it is settled
    # first and the wall is thickened to survive it.
    out["oval"] = min(max(out["oval"], 0.0), 0.5)
    room = 1.0 - out["oval"]
    out["wall_mm"] = min(max(out["wall_mm"], 1.25 / room), 8.0)
    out["width_mm"] = max(out["width_mm"], (900.0 / room) ** 0.5)
    out["plan_depth"] = min(max(out["plan_depth"], 0.0), 0.9)
    out["belly_pos"] = min(max(out["belly_pos"], 0.1), 0.9)
    out["base_frac"] = min(max(out["base_frac"], 0.3), 1.0)
    out["top_frac"] = min(max(out["top_frac"], 0.25), 1.0)

    # --- the candle comes first ---
    # The socket keeps its diameter and the body grows to take it. The order is
    # the design's order of importance: widen the holder, then flatten the plan,
    # and only if neither is allowed to move does the candle get smaller.
    socket = min(max(out["socket_d_mm"], 12.0), 90.0)
    for _ in range(4):
        low = plan_low(out["plan"], out["plan_depth"], out["plan_points"])
        deep = out["socket_depth_mm"] > out["height_mm"] * (1.0 - out["belly_pos"])
        bore = min(out["base_frac"], out["top_frac"]) if deep else out["top_frac"]
        held = out["width_mm"] / 2 * bore * low * room
        need = socket / 2 + out["wall_mm"] + 0.6
        if held >= need:
            break
        wider = min(220.0, out["width_mm"] * need / max(held, 1e-6))
        if wider > out["width_mm"] + 1e-6:
            out["width_mm"] = wider
            continue
        if out["plan_depth"] > 0.02:
            out["plan_depth"] = max(0.0, out["plan_depth"] - 0.2)
            continue
        socket = max(12.0, held * 2 - out["wall_mm"] * 2)
        break
    out["socket_d_mm"] = socket

    _settle(out)
    height = out["height_mm"]

    # --- the ribs ---
    if out["rib_count"] >= 3 and out["rib_mm"] > 0.15:
        out["rib_rows"] = min(max(int(out["rib_rows"]), 1), 6)
        out["rib_width"] = min(max(out["rib_width"], 0.2), 0.95)
        # A rib narrower than two beads once the squash has had its share is
        # not a rib the nozzle can lay: widen it, and if it cannot widen far
        # enough, drop the count until it can.
        while (
            out["rib_count"] > 4
            and out["rib_width"] * math.pi * out["width_mm"] / out["rib_count"] * room < 2.2
        ):
            out["rib_count"] -= 1
        # A rib arriving at full depth over one layer is a ledge. Run as one
        # column it ramps over the first eighth of the height; broken into rows
        # it rises over half a row. Both are the same 45 degree rule.
        rows = max(out["rib_rows"], 1)
        out["rib_mm"] = min(out["rib_mm"], height * 0.115 * rows, height / (2.1 * rows))
        # What the surface costs to mesh. The count dominates and carries a
        # floor of its own, so when even a flat rib will not fit the budget it
        # is the count that gives way; otherwise the depth does, which costs
        # the design less.
        # 38 against the template's 40: the depth is rounded to two decimals on
        # the way out, and a load sitting exactly on the limit rounds past it.
        load = (1.0 + 2.0 * out["oval"]) * (1.0 + out["plan_depth"])
        budget = 38.0 / max(load, 1e-6)
        while out["rib_count"] > 4 and out["rib_count"] * 0.6 > budget:
            out["rib_count"] -= 1
        arc = out["rib_width"] * math.pi * out["width_mm"] / max(out["rib_count"], 1)
        steep = budget / max(out["rib_count"], 1)
        if 2 * out["rib_mm"] / max(arc, 1e-6) > steep:
            out["rib_mm"] = _down(max(0.0, steep * arc / 2))
    else:
        out["rib_count"] = 0

    # --- the windows ---
    if out["pierce_count"] >= 1:
        # A plan that comes to a point and a wall that is cut through do not go
        # together: see the precondition of the same name.
        out["plan_depth"] = min(out["plan_depth"], 0.6)
        out["pierce_lo"] = min(
            max(out["pierce_lo"], out["floor_mm"] / max(height, 1e-6) + 0.02), 0.6
        )
        out["pierce_hi"] = min(max(out["pierce_hi"], out["pierce_lo"] + 0.18), 0.95)
        out["pierce_w"] = min(max(out["pierce_w"], 0.1), 0.8)
        # The pillar between two windows is a printed wall, so it gets the same
        # floor a wall gets. Fewer, wider windows beat more that cannot print.
        while (
            out["pierce_count"] > 3
            and (1 - out["pierce_w"]) * math.pi * out["width_mm"]
            / out["pierce_count"] * room < 2.8
        ):
            out["pierce_count"] -= 1
        gap = (1 - out["pierce_w"]) * math.pi * out["width_mm"] / max(out["pierce_count"], 1)
        if gap * room < 2.8:
            out["pierce_w"] = max(
                0.1, 1.0 - 2.8 * out["pierce_count"] / (math.pi * out["width_mm"] * room)
            )

    # --- the standing back ---
    if out["back"] != "none":
        out["back_t_mm"] = min(max(out["back_t_mm"], 4.0), 20.0)
        # The socket has to fit in front of the back, inside the footprint.
        need = out["socket_d_mm"] + out["back_t_mm"] * 2 + 8.0
        span = out["width_mm"] * out["base_frac"] * room
        if span < need:
            out["width_mm"] = min(220.0, out["width_mm"] * need / max(span, 1e-6))
            span = out["width_mm"] * out["base_frac"] * room
        if span < need:
            out["back_t_mm"] = max(4.0, (span - out["socket_d_mm"] - 8.0) / 2)

    out["height_mm"] = min(max(out["height_mm"], 14.0), 120.0)
    out["width_mm"] = min(max(out["width_mm"], 40.0), 220.0)
    # The rules below are the ones stated against the width and the height, and
    # the two rules above are the last things allowed to change either. Run
    # again here rather than trusted from before: the back can widen the body
    # and a crescent can raise it, and a silhouette settled against the old
    # numbers is a silhouette settled against numbers that no longer hold.
    _settle(out)
    return out


def _settle(out: dict[str, Any]) -> None:
    """The rules that read the body's overall size, applied in place.

    Every value a rule here reads is quantised to the two decimals the
    parameters are rounded to on the way out, and every value a rule here
    *writes* is rounded away from its own limit, so that the rounding at the end
    cannot move a satisfied inequality off the edge. A margin alone will not do
    it: half a hundredth of a wide body is a whole millimetre of overhang, which
    on a squat holder is most of what the rule had to give.
    """
    for name in ("width_mm", "height_mm", "belly_pos", "floor_mm", "back_h_mm"):
        out[name] = round(float(out[name]), 2)
    height = out["height_mm"]
    # Something under the candle, and something left of the body under that.
    out["floor_mm"] = round(min(max(out["floor_mm"], 1.5), height * 0.6, 10.0), 2)
    out["socket_depth_mm"] = _down(
        # A tenth of a millimetre under the floor, because 14.98 + 5.15 is not
        # 20.13 in binary and the rule that reads it is an inequality.
        min(max(out["socket_depth_mm"], 4.0), height - out["floor_mm"] - 0.1, 110.0)
    )
    if out["socket_depth_mm"] < 4.0:
        out["height_mm"] = round(min(120.0, out["floor_mm"] + 4.4), 2)
        out["socket_depth_mm"] = 4.0
        height = out["height_mm"]
    # Neither segment of the silhouette may open outward, or close in, faster
    # than 45 degrees. Fixed by moving the end that is out of reach, not the
    # width: the width is what the socket rule settled.
    reach_low = 1.92 * out["belly_pos"] * height / max(out["width_mm"], 1e-6)
    reach_high = 1.92 * (1.0 - out["belly_pos"]) * height / max(out["width_mm"], 1e-6)
    out["base_frac"] = min(1.0, _up(max(out["base_frac"], 1.0 - reach_low)))
    out["top_frac"] = min(1.0, _up(max(out["top_frac"], 1.0 - reach_high)))
    # A standing back clears the body it stands on, and a crescent -- which
    # opens outward faster than 45 degrees until it is 0.293 of its radius up --
    # is capped so the body is deep enough to bury that much of it.
    if out["back"] != "none":
        out["back_h_mm"] = min(_up(max(out["back_h_mm"], height + 14.0)), 160.0)
        if out["back"] == "crescent":
            out["back_h_mm"] = _down(min(out["back_h_mm"], height / 0.16))


def _up(value: float) -> float:
    """The value rounded away from zero to the hundredth the schema keeps."""
    return math.ceil(value * 100.0 - 1e-9) / 100.0


def _down(value: float) -> float:
    return math.floor(value * 100.0 + 1e-9) / 100.0


@DEFINITION.component("feasible", "bounds", name="params")
def _params(params: dict[str, Any], bounds: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Clamp to the template schema and round to sensible slider positions."""
    return {
        name: _fit(params.get(name, spec.get("default")), spec) for name, spec in bounds.items()
    }


def _fit(value: Any, spec: dict[str, Any]) -> Any:
    kind = spec.get("type")
    if spec.get("enum"):
        return value if value in spec["enum"] else spec.get("default", spec["enum"][0])
    if kind == "string":
        return str(value)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return spec.get("default")
    low, high = spec.get("minimum"), spec.get("maximum")
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return round(value) if kind == "integer" else round(float(value), 2)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_BOUNDS: dict[str, dict[str, Any]] | None = None


def style_names() -> tuple[str, ...]:
    return tuple(STYLES)


def schema_bounds() -> dict[str, dict[str, Any]]:
    """The template's parameter schema, loaded once."""
    global _BOUNDS
    if _BOUNDS is None:
        from ..registry import TemplateRegistry  # noqa: PLC0415

        _BOUNDS = dict(TemplateRegistry.load(strict=False).get(TEMPLATE_ID).properties)
    return _BOUNDS


def solve(
    seed: int = 1,
    *,
    style: str = "heart",
    variation: float = 0.55,
    overrides: dict[str, Any] | None = None,
) -> Solution:
    """Run the definition. The solution carries every intermediate value."""
    if style not in STYLES and style != "mixed":
        raise ValueError(f"unknown style {style!r}; known: {', '.join(STYLES)}, mixed")
    solution = DEFINITION.solve(
        style=style, seed=int(seed), variation=variation, overrides=overrides or {}
    )
    solution.values["params"]["seed"] = int(seed) % 10000
    return solution


def specimen(
    seed: int = 1,
    *,
    style: str = "heart",
    variation: float = 0.55,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One holder's parameters. Same arguments, same holder."""
    return dict(solve(seed, style=style, variation=variation, overrides=overrides)["params"])


def variations(
    count: int = 6,
    *,
    seed: int = 1,
    style: str = "mixed",
    variation: float = 0.55,
    overrides: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """A table of distinct holders from one seed."""
    return [
        specimen(
            member_seed(seed, index), style=style, variation=variation, overrides=overrides
        )
        for index in range(max(0, count))
    ]


def member_seed(seed: int, index: int) -> int:
    """The seed for member `index` of the set grown from `seed`."""
    return int(unit(seed, f"holder:member:{index}") * 10000)


def describe(params: dict[str, Any]) -> str:
    """A one-line label: how big it is, what it holds, what it wears."""
    notes = []
    if params.get("plan", "round") != "round" and params.get("plan_depth", 0) > 0.02:
        shape = params["plan"]
        if shape in ("star", "petal", "polygon"):
            shape = f"{params.get('plan_points', 6)}-point {shape}"
        notes.append(shape)
    if params.get("rib_count", 0) >= 3 and params.get("rib_mm", 0) > 0.15:
        rows = params.get("rib_rows", 1)
        notes.append(
            f"{params['rib_count']} ribs {params['rib_mm']:.1f} mm"
            + (f" in {rows} rows" if rows > 1 else "")
        )
    if params.get("pierce_count", 0) >= 1:
        notes.append(f"{params['pierce_count']} windows")
    if params.get("back", "none") != "none":
        notes.append(f"{params['back']} back {params['back_h_mm']:.0f} mm tall")
    if not notes:
        notes.append("plain")
    return (
        f"{params.get('width_mm', 0):.0f} mm across, {params.get('height_mm', 0):.0f} mm tall, "
        f"{params.get('socket_d_mm', 0):.1f} mm socket {params.get('socket_depth_mm', 0):.0f} mm "
        f"deep, " + ", ".join(notes)
    )
