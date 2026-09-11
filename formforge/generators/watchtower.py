"""The watchtower definition: a style and a seed in, a parameter set out.

The fifth generator on the same solver, and the one whose domain rule is the
candle holder's read backwards. There, one dimension refused to scale because a
tealight is a tealight. Here *everything structural* scales -- a tower built
half the size is half as wide, its roof is half as tall, its windows are half
as big -- and what refuses to scale is the **detail**: a clapboard course, the
relief on it, the depth of a window recess, the thickness of a deck plate, the
width of a mullion. Those are all one nozzle wide at any size.

So a small tower is not a big tower with everything shrunk. It is a tower with
*fewer, coarser courses* -- because the courses keep their millimetre and the
wall has less height to spend on them -- and that is exactly the difference
between a model that prints and a model that comes out as a smooth cone with a
faint suggestion of siding on it.

    style -------> preset ------\\
    seed --------> draws --------> jittered --> proportioned --> feasible --> params
    variation ------------------/

The counts are the other half of the same idea. A bigger roof gets more shingle
courses, a bigger gallery more railing openings, a bigger skirt more brackets:
what scales with the size of a thing is how many features fit on it at a fixed
feature size, not how big each feature is.
"""

from __future__ import annotations

import math
from typing import Any

from .graph import Definition, Solution
from .mushroom import unit

TEMPLATE_ID = "terrain_watchtower"

# Every style is a set of slider positions, written as they are meant to be
# seen at the style's own height. The proportion node rescales the structure
# and leaves the detail alone.
STYLES: dict[str, dict[str, Any]] = {
    "watchman": {
        "height_mm": 150, "width_mm": 62, "sides": 8, "storeys": 3,
        "taper": 0.16, "storey_step": 0.1, "gallery": "all", "gallery_mm": 7,
        "rail_h_mm": 9, "rail_gaps": 24, "bracket_count": 8,
        "course_mm": 2.2, "course_depth_mm": 0.45, "window_style": "arch",
        "window_w_mm": 7, "window_h_mm": 12, "window_rows": 1, "window_bars": 1,
        "door_w_mm": 9, "roof_style": "spire", "roof_h_mm": 26, "eave_mm": 6,
        "roof_courses": 8, "finial_mm": 8, "base_style": "disc", "base_d_mm": 96,
    },
    "keep": {
        "height_mm": 110, "width_mm": 76, "sides": 4, "storeys": 2,
        "taper": 0.05, "storey_step": 0.04, "gallery": "top", "gallery_mm": 6,
        "rail_h_mm": 11, "rail_gaps": 12, "rail_open": 0.42, "bracket_count": 8,
        "course_mm": 4.0, "course_depth_mm": 0.7, "window_style": "slit",
        "window_w_mm": 4, "window_h_mm": 13, "window_rows": 1, "window_bars": 0,
        "door_w_mm": 12, "roof_style": "dome", "roof_h_mm": 16, "eave_mm": 2,
        "roof_courses": 5, "finial_mm": 4, "base_style": "rock", "base_d_mm": 108,
    },
    "lighthouse": {
        "height_mm": 190, "width_mm": 54, "sides": 12, "storeys": 2,
        "taper": 0.3, "storey_step": 0.06, "gallery": "top", "gallery_mm": 9,
        "rail_h_mm": 10, "rail_gaps": 30, "rail_open": 0.6, "bracket_count": 12,
        "course_mm": 3.0, "course_depth_mm": 0.35, "window_style": "square",
        "window_w_mm": 5, "window_h_mm": 9, "window_rows": 1, "window_bars": 0,
        "door_w_mm": 8, "roof_style": "bell", "roof_h_mm": 22, "eave_mm": 5,
        "roof_courses": 7, "finial_mm": 9, "base_style": "disc", "base_d_mm": 94,
    },
    "lancet": {
        "height_mm": 180, "width_mm": 50, "sides": 8, "storeys": 3,
        "taper": 0.12, "storey_step": 0.12, "gallery": "top", "gallery_mm": 6,
        "rail_h_mm": 8, "rail_gaps": 20, "rail_open": 0.65, "bracket_count": 8,
        "course_mm": 2.6, "course_depth_mm": 0.35, "window_style": "gothic",
        "window_w_mm": 6, "window_h_mm": 16, "window_rows": 1, "window_bars": 1,
        "door_w_mm": 8, "roof_style": "spire", "roof_h_mm": 34, "eave_mm": 3,
        "roof_courses": 10, "finial_mm": 12, "base_style": "disc", "base_d_mm": 86,
    },
    "pagoda": {
        "height_mm": 170, "width_mm": 70, "sides": 6, "storeys": 4,
        "taper": 0.1, "storey_step": 0.14, "gallery": "all", "gallery_mm": 10,
        "rail_h_mm": 7, "rail_gaps": 28, "rail_open": 0.62, "bracket_count": 8,
        "course_mm": 2.4, "course_depth_mm": 0.4, "window_style": "square",
        "window_w_mm": 6, "window_h_mm": 9, "window_rows": 1, "window_bars": 1,
        "door_w_mm": 9, "roof_style": "bell", "roof_h_mm": 24, "eave_mm": 9,
        "roof_courses": 9, "finial_mm": 10, "base_style": "disc", "base_d_mm": 110,
    },
    "bastion": {
        "height_mm": 92, "width_mm": 82, "sides": 6, "storeys": 1,
        "taper": 0.12, "storey_step": 0.0, "gallery": "none", "gallery_mm": 0,
        "rail_h_mm": 0, "rail_gaps": 12, "bracket_count": 0,
        "course_mm": 4.4, "course_depth_mm": 0.8, "window_style": "slit",
        "window_w_mm": 4, "window_h_mm": 14, "window_rows": 2, "window_bars": 0,
        "door_w_mm": 13, "roof_style": "dome", "roof_h_mm": 14, "eave_mm": 3,
        "roof_courses": 4, "finial_mm": 3, "base_style": "rock", "base_d_mm": 112,
    },
    "spindle": {
        "height_mm": 200, "width_mm": 44, "sides": 8, "storeys": 2,
        "taper": 0.2, "storey_step": 0.16, "gallery": "top", "gallery_mm": 8,
        "rail_h_mm": 9, "rail_gaps": 18, "rail_open": 0.7, "bracket_count": 8,
        "course_mm": 2.0, "course_depth_mm": 0.3, "window_style": "arch",
        "window_w_mm": 5, "window_h_mm": 10, "window_rows": 1, "window_bars": 0,
        "door_w_mm": 7, "roof_style": "spire", "roof_h_mm": 40, "eave_mm": 4,
        "roof_courses": 12, "finial_mm": 14, "base_style": "disc", "base_d_mm": 80,
    },
    "gatehouse": {
        "height_mm": 78, "width_mm": 70, "sides": 4, "storeys": 1,
        "taper": 0.06, "storey_step": 0.0, "gallery": "none", "gallery_mm": 0,
        "rail_h_mm": 0, "rail_gaps": 10, "bracket_count": 0,
        "course_mm": 3.6, "course_depth_mm": 0.6, "window_style": "arch",
        "window_w_mm": 8, "window_h_mm": 11, "window_rows": 1, "window_bars": 1,
        "door_w_mm": 18, "roof_style": "spire", "roof_h_mm": 23, "eave_mm": 5,
        "roof_courses": 5, "finial_mm": 4, "base_style": "disc", "base_d_mm": 100,
    },
    "belfry": {
        "height_mm": 140, "width_mm": 60, "sides": 8, "storeys": 2,
        "taper": 0.1, "storey_step": 0.08, "gallery": "top", "gallery_mm": 7,
        "rail_h_mm": 8, "rail_gaps": 22, "rail_open": 0.68, "bracket_count": 8,
        "course_mm": 2.8, "course_depth_mm": 0.4, "window_style": "arch",
        "window_w_mm": 10, "window_h_mm": 20, "window_rows": 1, "window_bars": 2,
        "door_w_mm": 10, "roof_style": "bell", "roof_h_mm": 22, "eave_mm": 6,
        "roof_courses": 8, "finial_mm": 8, "base_style": "disc", "base_d_mm": 92,
    },
    "obelisk": {
        "height_mm": 165, "width_mm": 58, "sides": 6, "storeys": 2,
        "taper": 0.34, "storey_step": 0.02, "gallery": "none", "gallery_mm": 0,
        "rail_h_mm": 0, "rail_gaps": 12, "bracket_count": 0,
        "course_mm": 3.4, "course_depth_mm": 0.5, "window_style": "slit",
        "window_w_mm": 4, "window_h_mm": 15, "window_rows": 1, "window_bars": 0,
        "door_w_mm": 9, "roof_style": "spire", "roof_h_mm": 30, "eave_mm": 0,
        "roof_courses": 0, "finial_mm": 6, "base_style": "rock", "base_d_mm": 90,
    },
    "roundhouse": {
        "height_mm": 120, "width_mm": 72, "sides": 12, "storeys": 2,
        "taper": 0.08, "storey_step": 0.1, "gallery": "all", "gallery_mm": 8,
        "rail_h_mm": 8, "rail_gaps": 32, "rail_open": 0.55, "bracket_count": 12,
        "course_mm": 2.4, "course_depth_mm": 0.4, "window_style": "square",
        "window_w_mm": 5, "window_h_mm": 8, "window_rows": 1, "window_bars": 0,
        "door_w_mm": 10, "roof_style": "dome", "roof_h_mm": 20, "eave_mm": 5,
        "roof_courses": 6, "finial_mm": 5, "base_style": "disc", "base_d_mm": 104,
    },
    "rookery": {
        "height_mm": 195, "width_mm": 64, "sides": 6, "storeys": 4,
        "taper": 0.12, "storey_step": 0.1, "gallery": "all", "gallery_mm": 6,
        "rail_h_mm": 7, "rail_gaps": 22, "rail_open": 0.6, "bracket_count": 8,
        "course_mm": 2.0, "course_depth_mm": 0.35, "window_style": "arch",
        "window_w_mm": 5, "window_h_mm": 8, "window_rows": 1, "window_bars": 0,
        "door_w_mm": 9, "roof_style": "spire", "roof_h_mm": 28, "eave_mm": 4,
        "roof_courses": 9, "finial_mm": 9, "base_style": "rock", "base_d_mm": 100,
    },
}

STYLE_NOTE = {
    "watchman": "the reference: three octagonal storeys, two galleries, a shingled spire",
    "keep": "square, heavily coursed, one battlemented deck under a low dome",
    "lighthouse": "twelve-sided and strongly tapered, with the gallery near the top",
    "lancet": "tall and narrow, gothic windows, a steep spire",
    "pagoda": "four six-sided storeys, wide eaves, a deck at every level",
    "bastion": "squat and six-sided, two rows of loopholes, no gallery at all",
    "spindle": "very tall, very thin, and mostly roof",
    "gatehouse": "one wide storey around a big arched door",
    "belfry": "two storeys under tall mullioned openings",
    "obelisk": "heavily tapered, smooth-roofed, nothing hanging off it",
    "roundhouse": "twelve-sided and squat, galleried at both levels, domed",
    "rookery": "four six-sided storeys of small windows under a tall spire",
}

# How far each slider may wander from its style.
JITTER: dict[str, tuple[str, float]] = {
    "height_mm": ("rel", 0.14),
    "taper": ("abs", 0.05),
    "storey_step": ("abs", 0.04),
    "gallery_t_mm": ("abs", 0.5),
    "rail_open": ("abs", 0.08),
    "course_mm": ("rel", 0.18),
    "course_depth_mm": ("abs", 0.12),
    "window_depth_mm": ("abs", 0.4),
    "window_w_mm": ("rel", 0.12),
    "window_h_mm": ("rel", 0.14),
    "door_w_mm": ("rel", 0.12),
    "roof_h_mm": ("rel", 0.16),
    "eave_mm": ("abs", 1.2),
    "finial_mm": ("rel", 0.25),
}

# Structure scales with the height of the tower. Detail does not -- see the
# module docstring -- and neither does anything the printer measures in beads.
PROPORTIONAL = (
    "width_mm",
    "base_d_mm",
    "gallery_mm",
)

# What a nozzle can lay, whatever size the tower is. These are the floors the
# feasibility node holds against everything the proportion node just did.
DETAIL_FLOOR = {
    "course_mm": 1.6,
    "course_depth_mm": 0.3,
    "window_depth_mm": 0.8,
    "gallery_t_mm": 1.8,
    "rail_h_mm": 4.0,
    "window_w_mm": 2.4,
    "window_h_mm": 4.0,
}

DEFINITION = Definition("watchtower")

DEFINITION.slider(
    "style", "watchman", choices=(*STYLES, "mixed"),
    doc="Which tower to start from; `mixed` picks one per seed.",
)
DEFINITION.slider(
    "seed", 1, low=0, high=9999, doc="Drives the jitter and the choice under `mixed`."
)
DEFINITION.slider(
    "variation", 0.55, low=0.0, high=1.0,
    doc="How far a tower may wander from its style. 0 rebuilds the style exactly.",
)
DEFINITION.slider(
    "overrides", {},
    doc="Slider values pinned by the caller; honoured unless the geometry cannot take them.",
)


def face_width(width_mm: float, sides: int) -> float:
    """How wide one face of the plan is, across the flat.

    The template states this as a linear approximation because its precondition
    language has no sine in it. Here there is one, so the real number is used
    and the approximation is only ever the more cautious of the two.
    """
    return width_mm * math.sin(math.pi / max(int(sides), 3))


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
    keys = (*JITTER, *PROPORTIONAL, "rail_gaps", "bracket_count", "roof_courses", "style")
    return {key: unit(seed, "watchtower:" + key) for key in keys}


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
    """Rebuild what belongs to the tower's size, and count the features.

    Three counts scale here rather than three sizes: shingle courses with the
    roof, railing openings with the deck they run round, brackets with the
    skirt they are cut into. A bigger tower is not one with bigger shingles.
    """
    out = dict(jittered)
    pinned = set(overrides or ())
    style = STYLES[preset["style"]]
    height = out["height_mm"]
    reference = style.get("height_mm", preset["height_mm"])
    scale = height / max(reference, 1e-6)

    for name in PROPORTIONAL:
        if name in pinned:
            continue
        wobble = 1.0 + 0.06 * variation * (draws[name] * 2.0 - 1.0)
        out[name] = style.get(name, preset[name]) * scale * wobble

    counts = (
        ("rail_gaps", "gallery_mm", 4, 40),
        ("bracket_count", "width_mm", 0, 16),
        ("roof_courses", "roof_h_mm", 0, 24),
    )
    for name, _driver, low, high in counts:
        if name in pinned:
            continue
        value = style.get(name, preset[name])
        if value < 1:
            continue
        wobble = 0.92 + 0.16 * draws[name]
        out[name] = int(min(high, max(low, round(value * scale * wobble))))
    return out


@DEFINITION.component("proportioned", name="feasible")
def _feasible(params: dict[str, Any]) -> dict[str, Any]:
    """Satisfy the template's preconditions, and the CPU budget behind them."""
    out = dict(params)
    out["sides"] = int(min(max(out["sides"], 4), 12))
    out["storeys"] = int(min(max(out["storeys"], 1), 4))
    out["height_mm"] = min(max(out["height_mm"], 60.0), 245.0)
    out["width_mm"] = min(max(out["width_mm"], 26.0), 120.0)
    out["taper"] = min(max(out["taper"], 0.0), 0.45)
    out["storey_step"] = min(max(out["storey_step"], 0.0), 0.35)

    # --- detail keeps its millimetre, whatever the tower is doing ---
    for name, floor in DETAIL_FLOOR.items():
        out[name] = max(out[name], floor)
    out["course_depth_mm"] = min(out["course_depth_mm"], out["course_mm"], 1.2)
    out["window_depth_mm"] = min(out["window_depth_mm"], out["width_mm"] * 0.14, 4.0)
    out["gallery_t_mm"] = min(out["gallery_t_mm"], 6.0)
    out["eave_mm"] = min(max(out["eave_mm"], 0.0), 14.0)
    out["finial_mm"] = min(max(out["finial_mm"], 0.0), 24.0)
    out["width_mm"] = max(out["width_mm"], 26.0)

    # --- the height budget: base, eaves, roof and finial first, storeys last ---
    base_h = 0.0 if out["base_style"] == "none" else out["base_d_mm"] * 0.06
    for _ in range(4):
        spare = out["height_mm"] - out["roof_h_mm"] - out["finial_mm"] - out["eave_mm"] - base_h
        need = out["storeys"] * 17.0
        if spare >= need:
            break
        # Take it off the roof first: a shorter spire is still the same tower,
        # where a storey too short to put a window in is not.
        room = out["roof_h_mm"] + out["finial_mm"]
        want = need - spare
        if room - want >= out["width_mm"] * 0.3 or out["roof_style"] == "dome":
            share = want / max(room, 1e-6)
            out["roof_h_mm"] = max(4.0, out["roof_h_mm"] * (1.0 - share))
            out["finial_mm"] = max(0.0, out["finial_mm"] * (1.0 - share))
            continue
        if out["height_mm"] < 245.0:
            out["height_mm"] = min(245.0, out["height_mm"] + want + 2.0)
            continue
        out["storeys"] = max(1, out["storeys"] - 1)
    if out["roof_style"] != "dome":
        out["roof_h_mm"] = max(out["roof_h_mm"], out["width_mm"] * 0.3)
    out["roof_h_mm"] = min(max(out["roof_h_mm"], 4.0), 70.0)

    storey_h = (
        out["height_mm"] - out["roof_h_mm"] - out["finial_mm"] - out["eave_mm"] - base_h
    ) / max(out["storeys"], 1)

    # --- the gallery has to fit inside the storey that carries it ---
    if out["gallery"] == "none":
        out["gallery_mm"] = 0.0
        out["rail_h_mm"] = 0.0
        out["bracket_count"] = 0
    else:
        out["gallery_mm"] = min(max(out["gallery_mm"], 2.0), 20.0)
        room = storey_h * 0.66
        stack = out["gallery_mm"] + out["gallery_t_mm"] + out["rail_h_mm"]
        if stack > room:
            share = room / max(stack, 1e-6)
            out["gallery_mm"] = max(2.0, out["gallery_mm"] * share)
            out["rail_h_mm"] = max(0.0, out["rail_h_mm"] * share)
            out["gallery_t_mm"] = max(1.5, out["gallery_t_mm"] * share)
        if out["rail_h_mm"] < 4.0:
            out["rail_h_mm"] = 0.0

    # A railing post and a bracket are both printed walls, so both get a
    # printed wall's floor. Fewer openings beats openings that cannot print.
    if out["rail_h_mm"] > 0:
        out["rail_open"] = min(max(out["rail_open"], 0.2), 0.85)
        deck = out["width_mm"] + out["gallery_mm"] * 2
        while (
            out["rail_gaps"] > 4
            and (1 - out["rail_open"]) * math.pi * deck / out["rail_gaps"] < 1.6
        ):
            out["rail_gaps"] -= 1
        if (1 - out["rail_open"]) * math.pi * deck / max(out["rail_gaps"], 1) < 1.6:
            out["rail_open"] = max(0.2, 1.0 - 1.6 * out["rail_gaps"] / (math.pi * deck))
    else:
        out["rail_h_mm"] = 0.0
    if out["bracket_count"] >= 1:
        while (
            out["bracket_count"] > 1
            and math.pi * out["width_mm"] / out["bracket_count"] * 0.45 < 1.7
        ):
            out["bracket_count"] -= 1

    # --- the windows ---
    if out["window_style"] != "none" and out["window_rows"] >= 1:
        out["window_rows"] = int(min(max(out["window_rows"], 1), 3))
        # One window per face per row, and every one of them is a tool the
        # kernel has to cut. Rows give way before faces do: a tower with fewer
        # sides is a different tower, one with fewer rows is the same tower.
        while out["window_rows"] > 1 and out["window_rows"] * out["sides"] * out["storeys"] > 26:
            out["window_rows"] -= 1
        if out["window_rows"] * out["sides"] * out["storeys"] > 26:
            out["storeys"] = max(1, int(26 / max(out["sides"], 1)))
        # A window fits on its face with a jamb either side, and its mullions
        # come out of its own width.
        widest = face_width(out["width_mm"], out["sides"]) * 0.66
        out["window_w_mm"] = min(max(out["window_w_mm"], 2.0), widest, 18.0)
        bars = int(min(max(out["window_bars"], 0), 3))
        while bars >= 1 and out["window_w_mm"] < (bars + 1) * 1.7 + bars * 1.25:
            bars -= 1
        out["window_bars"] = bars
        # Rows of windows fit inside the storey with something between them.
        tall = storey_h * 0.9 / out["window_rows"] - 4.0
        out["window_h_mm"] = min(max(out["window_h_mm"], 4.0), max(4.0, tall), 34.0)
    else:
        out["window_rows"] = 0
        out["window_bars"] = 0

    out["door_w_mm"] = min(
        max(out["door_w_mm"], 0.0), face_width(out["width_mm"], out["sides"]) * 0.66, 24.0
    )

    # --- the base holds the tower ---
    if out["base_style"] != "none":
        out["base_d_mm"] = min(
            max(out["base_d_mm"], out["width_mm"] + out["gallery_mm"] + 4.0), 170.0
        )
        if out["base_d_mm"] < out["width_mm"] + out["gallery_mm"]:
            out["width_mm"] = max(26.0, out["base_d_mm"] - out["gallery_mm"] - 2.0)
    return _round(out)


# Values that a rule wants *at least* this much of are rounded up; everything
# else is rounded down. Rounding them all one way and then correcting a few
# does nothing: the correction has already lost the digit it needed.
ROUND_UP = ("roof_h_mm", "height_mm", "base_d_mm")


def _round(out: dict[str, Any]) -> dict[str, Any]:
    """Quantise to the two decimals the parameters are rounded to on the way
    out, so that a rule satisfied here is still satisfied there."""
    for name, value in out.items():
        if isinstance(value, float):
            step = math.ceil if name in ROUND_UP else math.floor
            out[name] = step(value * 100.0) / 100.0
    return out


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
    style: str = "watchman",
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
    style: str = "watchman",
    variation: float = 0.55,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One tower's parameters. Same arguments, same tower."""
    return dict(solve(seed, style=style, variation=variation, overrides=overrides)["params"])


def variations(
    count: int = 6,
    *,
    seed: int = 1,
    style: str = "mixed",
    variation: float = 0.55,
    overrides: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """A skyline of distinct towers from one seed."""
    return [
        specimen(
            member_seed(seed, index), style=style, variation=variation, overrides=overrides
        )
        for index in range(max(0, count))
    ]


def member_seed(seed: int, index: int) -> int:
    """The seed for member `index` of the set grown from `seed`."""
    return int(unit(seed, f"watchtower:member:{index}") * 10000)


def describe(params: dict[str, Any]) -> str:
    """A one-line label: how big it is, how it is built, what it wears."""
    notes = [f"{params.get('storeys', 0)} storeys", f"{params.get('sides', 0)} sides"]
    if params.get("gallery", "none") != "none" and params.get("gallery_mm", 0) > 1:
        where = "every level" if params["gallery"] == "all" else "the top"
        notes.append(f"galleried at {where}")
    if params.get("window_style", "none") != "none" and params.get("window_rows", 0) >= 1:
        bars = params.get("window_bars", 0)
        notes.append(
            f"{params['window_style']} windows"
            + (f" with {bars} mullion{'s' if bars > 1 else ''}" if bars else "")
        )
    if params.get("course_mm", 0) > 0.4 and params.get("course_depth_mm", 0) > 0.05:
        notes.append(f"{params['course_mm']:.1f} mm courses")
    notes.append(f"{params.get('roof_style', 'spire')} roof")
    return (
        f"{params.get('height_mm', 0):.0f} mm tall, {params.get('width_mm', 0):.0f} mm across, "
        + ", ".join(notes)
    )
