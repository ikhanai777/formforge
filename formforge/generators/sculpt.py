"""The sculptural vase definition: a style and a seed in, a parameter set out.

The third generator on the same solver, and the one that says whether the
contract in `__init__.py` was worth writing: nothing here is new machinery.
What is new is the domain. Where `vase.py` moves a fluted wall around, this one
places *ribs* -- solid, with a smooth cavity behind them, standing off a turned
body, starting and stopping where they are told to up the height and wound round
as they climb -- squashes the plan to an oval, and finishes the mouth with a cut
rather than a plane.

    style -------> preset ------\\
    seed --------> draws --------> jittered --> proportioned --> feasible --> params
    variation ------------------/

The proportion node is the same idea as the fluted vase's -- diameters are
carried as ratios of the height so a taller vase grows in every direction -- and
the feasibility node is where the domain shows: a rib has to come out wide
enough to print after the vase is squashed, shallow enough that its run-out is
not an overhang, and there have to be few and gentle enough of them that the
surface still meshes inside the budget the pipeline gives it.
"""

from __future__ import annotations

from typing import Any

from .graph import Definition, Solution
from .mushroom import unit

TEMPLATE_ID = "vessel_sculpt_vase"

# Every style is a set of slider positions, written as they are meant to be
# seen at the style's own height. The proportion node rescales the diameters
# when the height moves.
STYLES: dict[str, dict[str, Any]] = {
    "pleat": {
        "height_mm": 190, "base_d_mm": 56, "belly_d_mm": 122, "belly_pos": 0.42,
        "neck_d_mm": 92, "neck_pos": 0.8, "mouth_d_mm": 104, "shoulder": 0.6,
        "fin_count": 14, "fin_mm": 5, "fin_width": 0.85, "fin_sharp": 0.25,
        "fin_end": 1.0, "fin_fade": 0.15, "sweep_deg": 55, "sweep_ease": 0.4,
        "rim_cut": "saddle", "rim_drop_mm": 28,
    },
    "blade": {
        "height_mm": 230, "base_d_mm": 62, "belly_d_mm": 110, "belly_pos": 0.3,
        "neck_d_mm": 34, "neck_pos": 0.8, "mouth_d_mm": 40, "shoulder": 0.5,
        "fin_count": 14, "fin_mm": 10, "fin_width": 0.32, "fin_sharp": 1.0,
        "fin_start": 0.02, "fin_end": 0.5, "fin_fade": 0.4,
        "rim_cut": "slant", "rim_drop_mm": 26,
    },
    "wave": {
        "height_mm": 200, "base_d_mm": 60, "belly_d_mm": 130, "belly_pos": 0.55,
        "neck_d_mm": 96, "neck_pos": 0.85, "mouth_d_mm": 92, "shoulder": 0.7,
        "oval": 0.45, "fin_count": 14, "fin_mm": 3.5, "fin_width": 0.8, "fin_sharp": 0.15,
        "fin_end": 1.0, "fin_fade": 0.2, "sweep_deg": 55, "sweep_ease": 1.0,
        "rim_cut": "slant", "rim_drop_mm": 16, "wall_mm": 2.0,
    },
    "column": {
        "height_mm": 210, "base_d_mm": 78, "belly_d_mm": 84, "belly_pos": 0.5,
        "neck_d_mm": 82, "neck_pos": 0.85, "mouth_d_mm": 86, "shoulder": 0.2,
        "fin_count": 14, "fin_mm": 4.5, "fin_width": 0.4, "fin_sharp": 0.9,
        "fin_end": 1.0, "fin_fade": 0.1,
    },
    "urn": {
        "height_mm": 190, "base_d_mm": 52, "belly_d_mm": 118, "belly_pos": 0.38,
        "neck_d_mm": 60, "neck_pos": 0.82, "mouth_d_mm": 72, "shoulder": 0.55,
        "fin_count": 14, "fin_mm": 6, "fin_width": 0.45, "fin_sharp": 0.6,
        "fin_end": 0.72, "fin_fade": 0.25,
    },
    "crown": {
        "height_mm": 200, "base_d_mm": 66, "belly_d_mm": 104, "belly_pos": 0.35,
        "neck_d_mm": 88, "neck_pos": 0.75, "mouth_d_mm": 100, "shoulder": 0.65,
        "fin_count": 14, "fin_mm": 5, "fin_width": 0.42, "fin_sharp": 0.8,
        "fin_start": 0.45, "fin_end": 1.0, "fin_fade": 0.2,
        "rim_cut": "saddle", "rim_drop_mm": 22,
    },
    "spindle": {
        "height_mm": 235, "base_d_mm": 48, "belly_d_mm": 88, "belly_pos": 0.45,
        "neck_d_mm": 44, "neck_pos": 0.85, "mouth_d_mm": 52, "shoulder": 0.7,
        "fin_count": 8, "fin_mm": 9, "fin_width": 0.5, "fin_sharp": 0.5,
        "fin_end": 0.9, "fin_fade": 0.3, "sweep_deg": 180, "sweep_ease": 0.5,
    },
    "shell": {
        "height_mm": 180, "base_d_mm": 66, "belly_d_mm": 140, "belly_pos": 0.5,
        "neck_d_mm": 118, "neck_pos": 0.8, "mouth_d_mm": 112, "shoulder": 0.8,
        "oval": 0.5, "fin_count": 0, "rim_cut": "saddle", "rim_drop_mm": 24,
        "wall_mm": 2.0,
    },
    "bud": {
        "height_mm": 140, "base_d_mm": 40, "belly_d_mm": 70, "belly_pos": 0.35,
        "neck_d_mm": 30, "neck_pos": 0.78, "mouth_d_mm": 34, "shoulder": 0.6,
        "fin_count": 12, "fin_mm": 4, "fin_width": 0.4, "fin_sharp": 0.7,
        "fin_end": 0.65, "fin_fade": 0.25, "rim_cut": "slant", "rim_drop_mm": 12,
        "wall_mm": 1.6,
    },
    "flare": {
        "height_mm": 175, "base_d_mm": 46, "belly_d_mm": 62, "belly_pos": 0.25,
        "neck_d_mm": 78, "neck_pos": 0.75, "mouth_d_mm": 116, "shoulder": 0.55,
        "fin_count": 14, "fin_mm": 5, "fin_width": 0.4, "fin_sharp": 0.85,
        "fin_start": 0.1, "fin_end": 0.8, "fin_fade": 0.3,
    },
    "carafe": {
        "height_mm": 220, "base_d_mm": 70, "belly_d_mm": 96, "belly_pos": 0.25,
        "neck_d_mm": 36, "neck_pos": 0.7, "mouth_d_mm": 44, "shoulder": 0.8,
        "fin_count": 14, "fin_mm": 7, "fin_width": 0.5, "fin_sharp": 0.4,
        "fin_start": 0.05, "fin_end": 0.45, "fin_fade": 0.35, "sweep_deg": 30,
        "rim_cut": "slant", "rim_drop_mm": 30,
    },
    "drum": {
        "height_mm": 120, "base_d_mm": 88, "belly_d_mm": 144, "belly_pos": 0.45,
        "neck_d_mm": 128, "neck_pos": 0.85, "mouth_d_mm": 132, "shoulder": 0.5,
        "fin_count": 14, "fin_mm": 4, "fin_width": 0.42, "fin_sharp": 1.0,
        "fin_end": 1.0, "fin_fade": 0.15, "fin_vary": 0.4,
    },
}

STYLE_NOTE = {
    "pleat": "wide pleats wound half a turn, under a saddled rim",
    "blade": "deep thin blades on the body, a long throat, a beaked mouth",
    "wave": "squashed flat, with broad ridges swept across it",
    "column": "straight-sided, ribbed the whole way up",
    "urn": "the classic belly, with blades that stop at the shoulder",
    "crown": "bare below, bladed above, scooped at the rim",
    "spindle": "tall and narrow, eight deep fins wound half a turn",
    "shell": "no fins at all -- an oval turned form with a scooped mouth",
    "bud": "small, for one stem, cut on the slant",
    "flare": "a narrow foot opening into a wide fluted mouth",
    "carafe": "wide shoulders, bladed low, a beak on top",
    "drum": "squat and wide, with sixteen short ribs",
}

# How far each slider may wander from its style. Tight, like the fluted vase's:
# proportion is the design, and the fins are the second half of it.
JITTER: dict[str, tuple[str, float]] = {
    "height_mm": ("rel", 0.14),
    "belly_pos": ("abs", 0.05),
    "neck_pos": ("abs", 0.04),
    "shoulder": ("abs", 0.16),
    "oval": ("abs", 0.08),
    "fin_mm": ("rel", 0.22),
    "fin_width": ("abs", 0.08),
    "fin_sharp": ("abs", 0.15),
    "fin_start": ("abs", 0.04),
    "fin_end": ("abs", 0.06),
    "fin_fade": ("abs", 0.08),
    "fin_vary": ("abs", 0.2),
    "sweep_deg": ("rel", 0.35),
    "sweep_ease": ("abs", 0.2),
    "rim_drop_mm": ("rel", 0.25),
    "wall_mm": ("abs", 0.15),
}

# Diameters are not jittered on their own -- they are rebuilt from the style's
# own ratios against whatever height came out of the jitter.
PROPORTIONAL = ("base_d_mm", "belly_d_mm", "neck_d_mm", "mouth_d_mm")

DEFINITION = Definition("sculpt")

DEFINITION.slider(
    "style", "urn", choices=(*STYLES, "mixed"),
    doc="Which silhouette and fin treatment to start from; `mixed` picks one per seed.",
)
DEFINITION.slider(
    "seed", 1, low=0, high=9999, doc="Drives the jitter and the choice under `mixed`."
)
DEFINITION.slider(
    "variation", 0.55, low=0.0, high=1.0,
    doc="How far a vase may wander from its style. 0 rebuilds the style exactly.",
)
DEFINITION.slider(
    "overrides", {},
    doc="Slider values pinned by the caller; honoured unless the geometry cannot take them.",
)


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
    keys = (*JITTER, *PROPORTIONAL, "fin_count", "style")
    return {key: unit(seed, "sculpt:" + key) for key in keys}


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
    """Rebuild the diameters from the style's own ratios, and count the ribs.

    A vase that grew 14% taller grows in every direction at once, and it grows
    *more ribs* rather than wider ones: a rib is a printed wall with a width the
    nozzle cares about, so the thing that scales with the size of the vase is
    how many of them fit round it at the same spacing.
    """
    out = dict(jittered)
    pinned = set(overrides or ())
    style = STYLES[preset["style"]]
    height = out["height_mm"]
    reference = style.get("height_mm", preset["height_mm"])

    for name in PROPORTIONAL:
        if name in pinned:
            continue
        ratio = style.get(name, preset[name]) / max(reference, 1e-6)
        wobble = 1.0 + 0.07 * variation * (draws[name] * 2.0 - 1.0)
        out[name] = height * ratio * wobble

    count = style.get("fin_count", preset["fin_count"])
    if count >= 3 and "fin_count" not in pinned:
        pitch = style.get("belly_d_mm", 110) / max(count, 1)
        out["fin_count"] = max(4, round(out["belly_d_mm"] / pitch * (0.92 + 0.16 * draws["fin_count"])))
    return out


@DEFINITION.component("proportioned", name="feasible")
def _feasible(params: dict[str, Any]) -> dict[str, Any]:
    """Satisfy the template's preconditions, and the CPU budget behind them."""
    out = dict(params)

    # The squash comes off everything measured across it, so it is settled
    # first and the wall is thickened to survive it.
    out["oval"] = min(max(out["oval"], 0.0), 0.55)
    room = 1.0 - out["oval"]
    out["wall_mm"] = max(out["wall_mm"], 0.95 / room)

    # The neck sits above the belly, and both stay off the ends.
    out["belly_pos"] = min(max(out["belly_pos"], 0.16), 0.72)
    out["neck_pos"] = min(max(out["neck_pos"], out["belly_pos"] + 0.08), 0.95)

    # The cavity has to fit inside the narrowest diameter, and the footprint
    # has to hold the plate -- both of them after the squash.
    floor = (out["wall_mm"] * 2 + 7.0) / room
    for name in PROPORTIONAL:
        out[name] = max(out[name], floor)
    out["base_d_mm"] = max(out["base_d_mm"], (480.0 / room) ** 0.5)

    # No segment of the silhouette turns faster than 45 degrees, in either
    # direction. Walked bottom-up, so each fix is measured against the segment
    # below it that has already been fixed.
    height = out["height_mm"]
    spans = (
        ("base_d_mm", "belly_d_mm", out["belly_pos"]),
        ("belly_d_mm", "neck_d_mm", out["neck_pos"] - out["belly_pos"]),
        ("neck_d_mm", "mouth_d_mm", 1.0 - out["neck_pos"]),
    )
    for lower, upper, span in spans:
        allowed = span * height * 1.92
        delta = out[upper] - out[lower]
        if abs(delta) > allowed:
            out[upper] = out[lower] + (allowed if delta > 0 else -allowed)
        out[upper] = max(out[upper], floor)

    # --- the ribs ---
    out["fin_start"] = min(max(out["fin_start"], 0.0), 0.78)
    out["fin_end"] = min(max(out["fin_end"], out["fin_start"] + 0.12), 1.0)
    if out["fin_count"] >= 3:
        out["fin_width"] = min(max(out["fin_width"], 0.3), 0.9)
        # A rib narrower than two beads once the squash has had its share is
        # not a rib the nozzle can lay: widen it, and if it cannot widen far
        # enough, drop the count until it can.
        while (
            out["fin_count"] > 4
            and out["fin_width"] * 3.14159 * out["belly_d_mm"] / out["fin_count"] * room < 2.2
        ):
            out["fin_count"] -= 1
        # A rib rising to full depth faster than 45 degrees is a ledge.
        ramp = out["fin_fade"] * (out["fin_end"] - out["fin_start"]) * height
        out["fin_mm"] = min(out["fin_mm"], ramp)
        # What the surface costs to mesh: how steep each rib is against its own
        # width, times how many of them, times what the squash does to the
        # curvature. Depth is the cheapest thing to give up.
        def steep(depth):
            arc = out["fin_width"] * 3.14159 * out["belly_d_mm"] / max(out["fin_count"], 1)
            return 2 * depth / max(arc, 1e-6)

        squash = 1.0 + 2.0 * out["oval"]
        budget = 46.0 / (out["fin_count"] * squash)
        if steep(out["fin_mm"]) > budget:
            arc = out["fin_width"] * 3.14159 * out["belly_d_mm"] / max(out["fin_count"], 1)
            out["fin_mm"] = max(0.0, budget * arc / 2)
        # And the same again for a rib that also winds round as it climbs.
        # Giving up sweep costs the design less than giving up ribs.
        load = out["fin_count"] ** 2 * squash * max(0.5, steep(out["fin_mm"]))
        if abs(out["sweep_deg"]) * load > 14000:
            sign = 1 if out["sweep_deg"] >= 0 else -1
            out["sweep_deg"] = sign * (14000 / load)
    else:
        out["fin_count"] = 0

    # --- the mouth ---
    if out["rim_cut"] != "flat":
        out["rim_drop_mm"] = min(
            out["rim_drop_mm"], (1.0 - out["neck_pos"]) * height * 0.85
        )
        if out["rim_cut"] == "saddle":
            out["rim_drop_mm"] = min(out["rim_drop_mm"], out["mouth_d_mm"] * 0.42)

    out["base_mm"] = min(out["base_mm"], height * 0.22)
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
    style: str = "urn",
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
    style: str = "urn",
    variation: float = 0.55,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One vase's parameters. Same arguments, same vase."""
    return dict(solve(seed, style=style, variation=variation, overrides=overrides)["params"])


def variations(
    count: int = 6,
    *,
    seed: int = 1,
    style: str = "mixed",
    variation: float = 0.55,
    overrides: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """A shelf of distinct vases from one seed."""
    return [
        specimen(
            member_seed(seed, index), style=style, variation=variation, overrides=overrides
        )
        for index in range(max(0, count))
    ]


def member_seed(seed: int, index: int) -> int:
    """The seed for member `index` of the shelf grown from `seed`."""
    return int(unit(seed, f"sculpt:member:{index}") * 10000)


def describe(params: dict[str, Any]) -> str:
    """A one-line label: how big it is, what it wears, how it is cut."""
    notes = []
    if params.get("fin_count", 0) >= 3 and params.get("fin_mm", 0) > 0.15:
        notes.append(
            f"{params['fin_count']} fins {params['fin_mm']:.1f} mm deep"
            + (f", swept {params['sweep_deg']:.0f}°" if abs(params.get("sweep_deg", 0)) >= 10 else "")
        )
    else:
        notes.append("plain")
    if params.get("oval", 0) >= 0.05:
        notes.append(f"squashed to {(1 - params['oval']) * 100:.0f}%")
    if params.get("rim_cut", "flat") != "flat" and params.get("rim_drop_mm", 0) > 0.5:
        notes.append(f"{params['rim_cut']} rim {params['rim_drop_mm']:.0f} mm deep")
    widest = max(params.get(k, 0) for k in PROPORTIONAL)
    return (
        f"{params.get('height_mm', 0):.0f} mm tall, {widest:.0f} mm across, "
        f"{params.get('wall_mm', 0):.1f} mm wall, " + ", ".join(notes)
    )
