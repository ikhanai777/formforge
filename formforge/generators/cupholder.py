"""The cup holder definition: a style and a seed in, a parameter set out.

The sixth generator on the same solver. Its subject is a sleeve a cup drops
into, ornamented the way a cast one is, and it inherits the candle holder's
first rule wholesale -- **the socket is set by the cup, not by the design.** A
mug is 80 mm across whatever you wrap round it, so `bore_d_mm` is the one number
in this file that never scales with anything.

What is new here is the second rule, which is the watchtower's read the other
way round:

    the relief is millimetres; the repeats are millimetres of arc.

Make a holder wider and it does not get bigger acanthus leaves, it gets *more*
of them at the same size -- because a leaf is cast at the size a leaf is cast
at, and what changes with the circumference is how many go round. Relief depth,
bead depth, panel frame width and wall thickness are all measured against the
nozzle and stay put; `emboss_count` and `panels` are counts and follow the
circumference. That is the whole of `_proportioned`.

    style -------> preset ------\\
    seed --------> draws --------> jittered --> proportioned --> feasible --> params
    variation ------------------/

The third rule is a cost rule, and it is the one that shapes the styles most.
The ornament lives in a field sampled round each section, so the surface the
loft makes is as big as the finest thing in the pattern -- and every boolean
afterwards, the socket bore and the handle fuse, has to intersect its tool with
the whole of it. Two gigabytes of sandbox buys about 4,600 points-times-sections
with a handle on and 7,000 without, which is why a style that wants sixteen
repeats and three rows has to give up its handle, and why `_feasible` spends the
budget in that order.
"""

from __future__ import annotations

import math
from typing import Any

from .graph import Definition, Solution
from .mushroom import unit

TEMPLATE_ID = "tableware_cup_holder"

# Every style is a set of slider positions written at the style's own size. The
# socket is stated in each because it is the cup the style is for: 70 mm is a
# tea glass, 78 a mug, 88 the lip of a takeaway cup, 96 a pint.
STYLES: dict[str, dict[str, Any]] = {
    "podstakannik": {
        "height_mm": 104, "bore_d_mm": 72, "bore_taper": 0.16, "bore_depth": 0.86,
        "wall_mm": 3.4, "floor_mm": 0, "belly": 0.06, "waist": 0.12,
        "waist_pos": 0.6, "foot_mm": 9, "foot_h_mm": 9,
        "band_low": "guilloche", "band_mid": "acanthus", "band_high": "eggdart",
        "split_low": 0.2, "split_high": 0.82, "emboss_mm": 1.2, "emboss_count": 12,
        "emboss_rows": 1, "emboss_sharp": 0.7, "panels": 6, "panel_frame": 0.2,
        "bead_mm": 1.0, "handle_count": 1, "handle_mm": 22, "handle_lo": 0.18,
        "handle_hi": 0.9, "handle_t_mm": 7, "handle_pierce": 0.6,
    },
    "victorian": {
        "height_mm": 94, "bore_d_mm": 80, "bore_taper": 0.12, "bore_depth": 0.8,
        "wall_mm": 3.6, "floor_mm": 3.5, "belly": 0.12, "waist": 0.1,
        "waist_pos": 0.52, "foot_mm": 8, "foot_h_mm": 9,
        "band_low": "rope", "band_mid": "acanthus", "band_high": "eggdart",
        "split_low": 0.24, "split_high": 0.78, "emboss_mm": 1.2, "emboss_count": 12,
        "emboss_rows": 1, "emboss_sharp": 0.65, "panels": 6, "panel_frame": 0.22,
        "bead_mm": 1.1, "handle_count": 2, "handle_mm": 16, "handle_lo": 0.24,
        "handle_hi": 0.82, "handle_t_mm": 7, "handle_pierce": 0.5,
    },
    "doric": {
        "height_mm": 100, "bore_d_mm": 78, "bore_taper": 0.06, "bore_depth": 0.86,
        "wall_mm": 4.0, "floor_mm": 4, "belly": 0.02, "waist": 0.04,
        "waist_pos": 0.5, "foot_mm": 10, "foot_h_mm": 11,
        "band_low": "plain", "band_mid": "flute", "band_high": "plain",
        "split_low": 0.14, "split_high": 0.88, "emboss_mm": 1.6, "emboss_count": 16,
        "emboss_rows": 1, "emboss_sharp": 0.9, "panels": 0, "panel_frame": 0.18,
        "bead_mm": 1.4, "handle_count": 0,
    },
    "mug": {
        "height_mm": 82, "bore_d_mm": 84, "bore_taper": 0.1, "bore_depth": 0.78,
        "wall_mm": 3.2, "floor_mm": 3, "belly": 0.06, "waist": 0.04,
        "waist_pos": 0.5, "foot_mm": 5, "foot_h_mm": 6,
        "band_low": "bead", "band_mid": "plain", "band_high": "dentil",
        "split_low": 0.18, "split_high": 0.76, "emboss_mm": 1.0, "emboss_count": 14,
        "emboss_rows": 1, "emboss_sharp": 0.8, "panels": 0, "panel_frame": 0.18,
        "bead_mm": 0.9, "handle_count": 1, "handle_mm": 20, "handle_lo": 0.2,
        "handle_hi": 0.88, "handle_t_mm": 8, "handle_pierce": 0.55,
    },
    "sleeve": {
        "height_mm": 88, "bore_d_mm": 88, "bore_taper": 0.2, "bore_depth": 0.95,
        "wall_mm": 2.6, "floor_mm": 0, "belly": 0.0, "waist": 0.0,
        "waist_pos": 0.5, "foot_mm": 2, "foot_h_mm": 4,
        "band_low": "plain", "band_mid": "rope", "band_high": "plain",
        "split_low": 0.2, "split_high": 0.8, "emboss_mm": 0.9, "emboss_count": 14,
        "emboss_rows": 2, "emboss_sharp": 0.5, "panels": 0, "panel_frame": 0.18,
        "bead_mm": 0.7, "handle_count": 0,
    },
    "chalice": {
        "height_mm": 120, "bore_d_mm": 76, "bore_taper": 0.18, "bore_depth": 0.62,
        "wall_mm": 3.6, "floor_mm": 5, "belly": 0.18, "waist": 0.24,
        "waist_pos": 0.42, "foot_mm": 18, "foot_h_mm": 22,
        "band_low": "flute", "band_mid": "scallop", "band_high": "bead",
        "split_low": 0.3, "split_high": 0.8, "emboss_mm": 1.4, "emboss_count": 12,
        "emboss_rows": 1, "emboss_sharp": 0.55, "panels": 0, "panel_frame": 0.18,
        "bead_mm": 1.2, "handle_count": 0,
    },
    "lattice": {
        "height_mm": 96, "bore_d_mm": 78, "bore_taper": 0.12, "bore_depth": 0.84,
        "wall_mm": 3.4, "floor_mm": 3.5, "belly": 0.08, "waist": 0.06,
        "waist_pos": 0.55, "foot_mm": 7, "foot_h_mm": 8,
        "band_low": "bead", "band_mid": "lattice", "band_high": "bead",
        "split_low": 0.16, "split_high": 0.86, "emboss_mm": 1.2, "emboss_count": 14,
        "emboss_rows": 3, "emboss_sharp": 0.75, "panels": 0, "panel_frame": 0.18,
        "bead_mm": 1.0, "handle_count": 0,
    },
    "nautical": {
        "height_mm": 90, "bore_d_mm": 82, "bore_taper": 0.1, "bore_depth": 0.8,
        "wall_mm": 3.4, "floor_mm": 3.5, "belly": 0.08, "waist": 0.06,
        "waist_pos": 0.5, "foot_mm": 8, "foot_h_mm": 8,
        "band_low": "rope", "band_mid": "plain", "band_high": "rope",
        "split_low": 0.26, "split_high": 0.74, "emboss_mm": 1.5, "emboss_count": 10,
        "emboss_rows": 1, "emboss_sharp": 0.4, "panels": 0, "panel_frame": 0.18,
        "bead_mm": 1.3, "handle_count": 1, "handle_mm": 18, "handle_lo": 0.22,
        "handle_hi": 0.84, "handle_t_mm": 8, "handle_pierce": 0.45,
    },
    "tankard": {
        "height_mm": 86, "bore_d_mm": 92, "bore_taper": 0.08, "bore_depth": 0.78,
        "wall_mm": 3.8, "floor_mm": 4, "belly": 0.1, "waist": 0.12,
        "waist_pos": 0.58, "foot_mm": 10, "foot_h_mm": 11,
        "band_low": "dentil", "band_mid": "plain", "band_high": "bead",
        "split_low": 0.22, "split_high": 0.8, "emboss_mm": 1.3, "emboss_count": 14,
        "emboss_rows": 1, "emboss_sharp": 0.85, "panels": 0, "panel_frame": 0.18,
        "bead_mm": 1.2, "handle_count": 1, "handle_mm": 24, "handle_lo": 0.16,
        "handle_hi": 0.92, "handle_t_mm": 9, "handle_pierce": 0.65,
    },
    "shell": {
        "height_mm": 92, "bore_d_mm": 78, "bore_taper": 0.14, "bore_depth": 0.8,
        "wall_mm": 3.6, "floor_mm": 4, "belly": 0.16, "waist": 0.14,
        "waist_pos": 0.48, "foot_mm": 11, "foot_h_mm": 12,
        "band_low": "plain", "band_mid": "scallop", "band_high": "eggdart",
        "split_low": 0.18, "split_high": 0.8, "emboss_mm": 1.5, "emboss_count": 10,
        "emboss_rows": 1, "emboss_sharp": 0.5, "panels": 8, "panel_frame": 0.16,
        "bead_mm": 1.1, "handle_count": 0,
    },
    "caddy": {
        "height_mm": 128, "bore_d_mm": 70, "bore_taper": 0.04, "bore_depth": 0.92,
        "wall_mm": 3.2, "floor_mm": 4, "belly": 0.02, "waist": 0.02,
        "waist_pos": 0.5, "foot_mm": 7, "foot_h_mm": 8,
        "band_low": "dentil", "band_mid": "guilloche", "band_high": "dentil",
        "split_low": 0.16, "split_high": 0.84, "emboss_mm": 1.1, "emboss_count": 12,
        "emboss_rows": 3, "emboss_sharp": 0.7, "panels": 0, "panel_frame": 0.18,
        "bead_mm": 1.0, "handle_count": 0,
    },
    "laurel": {
        "height_mm": 98, "bore_d_mm": 76, "bore_taper": 0.14, "bore_depth": 0.82,
        "wall_mm": 3.4, "floor_mm": 3.5, "belly": 0.1, "waist": 0.16,
        "waist_pos": 0.56, "foot_mm": 9, "foot_h_mm": 10,
        "band_low": "eggdart", "band_mid": "plain", "band_high": "guilloche",
        "split_low": 0.28, "split_high": 0.74, "emboss_mm": 1.3, "emboss_count": 12,
        "emboss_rows": 1, "emboss_sharp": 0.6, "panels": 0, "panel_frame": 0.18,
        "bead_mm": 1.1, "handle_count": 2, "handle_mm": 14, "handle_lo": 0.3,
        "handle_hi": 0.76, "handle_t_mm": 6, "handle_pierce": 0.4,
    },
}

STYLE_NOTE = {
    "podstakannik": "the tea-glass holder: tall, open-bottomed, one big pierced handle",
    "victorian": "panelled cartouches between rope and egg-and-dart, a handle each side",
    "doric": "a fluted column with a heavy foot and nothing hanging off it",
    "mug": "wide and plain, a dentil cornice and one comfortable handle",
    "sleeve": "open at both ends, one rope band -- the one to print for a paper cup",
    "chalice": "a tall spreading foot under a bellied bowl of shells",
    "lattice": "a diagonal lattice the whole way up between bead mouldings",
    "nautical": "two heavy rope bands round a plain body",
    "tankard": "squat, wide and beaded, with a big loop handle",
    "shell": "scallops in framed panels over a bellied body",
    "caddy": "tall and straight, guilloche between dentils -- for pens as much as cups",
    "laurel": "narrow bands top and bottom, two small ears, a soft waist",
}

# How far each slider may wander from its style. Loose on the ornament, tight
# on anything the cup touches.
JITTER: dict[str, tuple[str, float]] = {
    "height_mm": ("rel", 0.12),
    "bore_taper": ("abs", 0.04),
    "bore_depth": ("abs", 0.06),
    "wall_mm": ("abs", 0.3),
    "floor_mm": ("abs", 0.8),
    "belly": ("abs", 0.05),
    "waist": ("abs", 0.06),
    "waist_pos": ("abs", 0.07),
    "foot_mm": ("rel", 0.25),
    "foot_h_mm": ("rel", 0.25),
    "split_low": ("abs", 0.04),
    "split_high": ("abs", 0.04),
    "emboss_mm": ("rel", 0.18),
    "emboss_sharp": ("abs", 0.15),
    "panel_frame": ("abs", 0.03),
    "bead_mm": ("rel", 0.2),
    "handle_mm": ("rel", 0.15),
    "handle_lo": ("abs", 0.04),
    "handle_hi": ("abs", 0.04),
    "handle_t_mm": ("abs", 0.8),
    "handle_pierce": ("abs", 0.1),
}

# What follows the height rather than being stated against it: how tall the foot
# is and how far a handle reaches. Not the socket -- see the module docstring --
# and not the relief, which is measured in nozzles.
PROPORTIONAL = ("foot_h_mm", "foot_mm", "handle_mm")

# Rules that are satisfied at a limit round off it when `_fit` takes two
# decimals, so these are rounded up rather than to nearest on the way out.
ROUND_UP = ("wall_mm", "height_mm")

DEFINITION = Definition("cupholder")

DEFINITION.slider(
    "style", "victorian", choices=(*STYLES, "mixed"),
    doc="Which body, registers and handles to start from; `mixed` picks one per seed.",
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


def surface_cost(count: int, panels: int, rows: int) -> int:
    """Points round times sections up, computed the way the template computes it.

    This is a memory figure, not a triangle figure: the loft is nearly free and
    each boolean against it is not, so what this number is compared against
    depends on whether there is a handle to fuse.
    """
    detail = max(int(count), int(panels) * 2, 8)
    points = min(96, max(60, detail * 6))
    sections = min(72, max(24, max(int(rows), 1) * 21))
    return points * sections


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
    keys = (*JITTER, *PROPORTIONAL, "emboss_count", "panels", "style")
    return {key: unit(seed, "cupholder:" + key) for key in keys}


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
    """Rebuild what belongs to the size, and re-count what goes round.

    Two different kinds of scaling meet here. The foot and the handle are parts
    of the body and follow its height. The repeats and the panels are *counts*,
    and a count follows the circumference at a fixed pitch -- a holder for a
    92 mm tankard gets more acanthus leaves than one for a 70 mm tea glass, not
    bigger ones. Everything else the ornament is made of is a millimetre figure
    and is left exactly where the style put it.
    """
    out = dict(jittered)
    pinned = set(overrides or ())
    style = STYLES[preset["style"]]
    height = out["height_mm"]
    reference = style.get("height_mm", preset["height_mm"])

    for name in PROPORTIONAL:
        if name in pinned or name not in out:
            continue
        ratio = style.get(name, preset[name]) / max(reference, 1e-6)
        wobble = 1.0 + 0.07 * variation * (draws[name] * 2.0 - 1.0)
        out[name] = height * ratio * wobble

    girth = math.pi * (out["bore_d_mm"] + out["wall_mm"] * 2)
    ref_girth = math.pi * (
        style.get("bore_d_mm", preset["bore_d_mm"]) + style.get("wall_mm", preset["wall_mm"]) * 2
    )
    for name in ("emboss_count", "panels"):
        count = style.get(name, preset[name])
        if count < 2 or name in pinned:
            continue
        pitch = ref_girth / max(count, 1)
        out[name] = max(2, round(girth / pitch * (0.94 + 0.12 * draws[name])))
    return out


@DEFINITION.component("proportioned", name="feasible")
def _feasible(params: dict[str, Any]) -> dict[str, Any]:
    """Satisfy the template's preconditions, and the memory budget behind them."""
    out = dict(params)

    # --- the cup comes first ---
    # The socket is the one number the design does not get to choose, so it is
    # settled before anything is measured against it.
    out["bore_d_mm"] = min(max(out["bore_d_mm"], 55.0), 105.0)
    out["bore_taper"] = min(max(out["bore_taper"], 0.0), 0.35)
    out["wall_mm"] = max(out["wall_mm"], 1.6)
    # A body narrower than 60 mm over the socket is not a holder for any cup.
    if out["bore_d_mm"] + out["wall_mm"] * 2 < 60.0:
        out["wall_mm"] = _up((60.0 - out["bore_d_mm"]) / 2)
    out["wall_mm"] = min(out["wall_mm"], 7.0)

    out["height_mm"] = min(max(out["height_mm"], 40.0), 170.0)
    out["belly"] = min(max(out["belly"], 0.0), 0.45)
    out["waist"] = min(max(out["waist"], 0.0), 0.45)
    out["waist_pos"] = min(max(out["waist_pos"], 0.25), 0.8)
    out["foot_mm"] = min(max(out["foot_mm"], 0.0), 26.0)
    out["foot_h_mm"] = min(max(out["foot_h_mm"], 0.0), 34.0)

    # --- the ornament is measured in nozzles ---
    out["emboss_mm"] = min(max(out["emboss_mm"], 0.0), out["wall_mm"] * 0.8, 3.0)
    out["bead_mm"] = min(max(out["bead_mm"], 0.0), out["wall_mm"] * 0.8, 3.0)
    out["emboss_sharp"] = min(max(out["emboss_sharp"], 0.0), 1.0)
    out["panel_frame"] = min(max(out["panel_frame"], 0.05), 0.4)
    out["emboss_count"] = int(min(max(out["emboss_count"], 4), 16))
    out["panels"] = int(min(max(out["panels"], 0), 8))
    out["emboss_rows"] = int(min(max(out["emboss_rows"], 1), 3))

    girth = math.pi * (out["bore_d_mm"] + out["wall_mm"] * 2)
    # One repeat has to be wider than two beads once it is drawn.
    while out["emboss_count"] > 4 and girth / out["emboss_count"] < 3.0:
        out["emboss_count"] -= 1
    # A panel frame is a printed wall.
    while out["panels"] >= 2 and out["panel_frame"] * girth / out["panels"] < 1.4:
        if out["panel_frame"] < 0.4:
            out["panel_frame"] = min(0.4, _up(1.4 * out["panels"] / girth))
            continue
        out["panels"] -= 1
    if out["panels"] < 2:
        out["panels"] = 0

    # --- the memory budget ---
    # Spent in the order the design cares least about: rows first, because a
    # register with two rows of the same motif reads much like one with three;
    # then the repeats; and only when neither can give way does the handle go.
    if out["handle_count"] >= 1:
        while (
            out["emboss_rows"] > 1
            and surface_cost(out["emboss_count"], out["panels"], out["emboss_rows"]) > 4600
        ):
            out["emboss_rows"] -= 1
        while (
            out["emboss_count"] > 4
            and surface_cost(out["emboss_count"], out["panels"], out["emboss_rows"]) > 4600
        ):
            out["emboss_count"] -= 1
            if out["panels"] * 2 > out["emboss_count"]:
                out["panels"] = max(0, out["panels"] - 1)
                if out["panels"] < 2:
                    out["panels"] = 0
        if surface_cost(out["emboss_count"], out["panels"], out["emboss_rows"]) > 4600:
            out["handle_count"] = 0
    while (
        out["emboss_rows"] > 1
        and surface_cost(out["emboss_count"], out["panels"], out["emboss_rows"]) > 7000
    ):
        out["emboss_rows"] -= 1

    _settle(out)

    # --- the handles ---
    if out["handle_count"] >= 1:
        out["handle_count"] = int(min(out["handle_count"], 2))
        out["handle_t_mm"] = min(max(out["handle_t_mm"], 3.0), 14.0)
        out["handle_pierce"] = min(max(out["handle_pierce"], 0.0), 0.8)
        out["handle_lo"] = min(max(out["handle_lo"], 0.05), 0.6)
        out["handle_hi"] = min(max(out["handle_hi"], _up(out["handle_lo"] + 0.25)), 0.98)
        if out["handle_hi"] - out["handle_lo"] < 0.25:
            out["handle_lo"] = _down(out["handle_hi"] - 0.25)
        # It goes out and back at 45 degrees, so its reach is bounded by half
        # the height it has to do it in.
        span = (out["handle_hi"] - out["handle_lo"]) * out["height_mm"]
        out["handle_mm"] = min(max(out["handle_mm"], 4.0), _down(span / 2), 40.0)
        if out["handle_mm"] < 4.0:
            out["handle_count"] = 0
    else:
        out["handle_count"] = 0

    _settle(out)
    return out


def _settle(out: dict[str, Any]) -> None:
    """The rules that read the height and the registers, applied in place.

    Every value read here is quantised to the two decimals `_fit` keeps, and
    every value written is rounded away from its own limit -- a rule satisfied
    exactly at a limit rounds off it otherwise, and the sweep finds that before
    anybody else does.
    """
    for name in ("height_mm", "floor_mm", "foot_h_mm", "foot_mm", "wall_mm", "bore_depth"):
        out[name] = round(float(out[name]), 2)
    height = out["height_mm"]

    # A register needs room for its rows, and each row needs enough height that
    # the motif in it is a shape rather than a scratch. The splits move rather
    # than the rows: a taller lower register is a design choice, a row three
    # millimetres tall is not a row.
    rows = max(int(out["emboss_rows"]), 1)
    out["split_low"] = min(max(out["split_low"], _up(rows * 3.0 / height), 0.08), 0.45)
    out["split_high"] = min(
        max(out["split_high"], _up(out["split_low"] + 0.3), 0.5),
        _down(1.0 - rows * 3.0 / height),
        0.95,
    )
    if out["split_high"] < out["split_low"] + 0.3:
        out["split_low"] = _down(max(0.08, out["split_high"] - 0.3))
    if out["split_high"] < out["split_low"] + 0.3:
        # Neither split can move far enough: the register is what has to give.
        out["emboss_rows"] = 1
        out["split_low"] = 0.24
        out["split_high"] = 0.8

    # The socket, the floor under it and the foot beside it all share the
    # height, and the floor is the one that yields.
    out["bore_depth"] = min(max(out["bore_depth"], 0.35), 0.95)
    out["floor_mm"] = min(max(out["floor_mm"], 0.0), 10.0)
    if out["floor_mm"] > 0.05:
        out["floor_mm"] = _down(min(out["floor_mm"], out["bore_depth"] * height - 2.2))
        if out["floor_mm"] < 1.5:
            out["floor_mm"] = 0.0
    out["foot_h_mm"] = _down(
        min(max(out["foot_h_mm"], 0.0), height + 1.8 - out["bore_depth"] * height, 34.0)
    )
    if out["foot_h_mm"] < 0.0:
        out["foot_h_mm"] = 0.0
        out["bore_depth"] = _down(min(out["bore_depth"], 1.0 + 1.8 / height))
    # The foot spreads on a cone, so how far out it may reach is set by how far
    # up it has to do it in. Settled last, because the line above is the last
    # thing allowed to shorten it.
    # 0.4 against the template's 0.5: 7.88 + 0.5 is 8.379999999999999 in binary,
    # and a foot rounded to exactly 8.38 is then one ulp over a rule it meets.
    out["foot_mm"] = _down(min(max(out["foot_mm"], 0.0), out["foot_h_mm"] + 0.4, 26.0))


def _up(value: float) -> float:
    """The value rounded away from zero to the hundredth the schema keeps."""
    return math.ceil(value * 100.0 - 1e-9) / 100.0


def _down(value: float) -> float:
    return math.floor(value * 100.0 + 1e-9) / 100.0


@DEFINITION.component("feasible", "bounds", name="params")
def _params(params: dict[str, Any], bounds: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Clamp to the template schema and round to sensible slider positions."""
    return {
        name: _fit(params.get(name, spec.get("default")), spec, name)
        for name, spec in bounds.items()
    }


def _fit(value: Any, spec: dict[str, Any], name: str = "") -> Any:
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
    if kind == "integer":
        return round(value)
    kept = _up(float(value)) if name in ROUND_UP else round(float(value), 2)
    if high is not None:
        kept = min(kept, high)
    return kept


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
    style: str = "victorian",
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
    style: str = "victorian",
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
    return int(unit(seed, f"cupholder:member:{index}") * 10000)


def describe(params: dict[str, Any]) -> str:
    """A one-line label: how big it is, what it holds, what it wears."""
    bands = [params.get(k, "plain") for k in ("band_low", "band_mid", "band_high")]
    worn = [b for b in bands if b != "plain"]
    notes = []
    if worn and params.get("emboss_mm", 0) > 0.05:
        notes.append(
            "/".join(worn) + f" {params['emboss_mm']:.1f} mm proud"
            f" x{params.get('emboss_count', 0)}"
        )
    if params.get("panels", 0) >= 2:
        notes.append(f"{params['panels']} panels")
    if params.get("bead_mm", 0) > 0.05:
        notes.append("bead mouldings")
    if params.get("handle_count", 0) >= 1:
        notes.append(
            f"{params['handle_count']} handle" + ("s" if params["handle_count"] > 1 else "")
        )
    if params.get("floor_mm", 0) <= 0.05:
        notes.append("open both ends")
    if not notes:
        notes.append("plain")
    return (
        f"{params.get('height_mm', 0):.0f} mm tall for a "
        f"{params.get('bore_d_mm', 0):.0f} mm cup, "
        f"{params.get('wall_mm', 0):.1f} mm wall, " + ", ".join(notes)
    )
