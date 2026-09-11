"""The vase definition: a style and a seed in, a parameter set out.

Same shape as the mushroom definition and the same solver, which is the point:
the second generator is where a graph either pays for itself or turns out to
have been scaffolding. What differs is entirely in the domain.

    style -------> preset ------\\
    seed --------> draws --------> jittered --> proportioned --> feasible --> params
    variation ------------------/

A vase's proportions are the design, far more than a mushroom's are: move the
belly up 10% and it stops being an urn. So the jitter here is deliberately
tighter than the mushroom's, and the proportion node works in *ratios of the
height* rather than in millimetres -- scale a vase up and every diameter goes
with it, which is what keeps a 250 mm version of a bud vase looking like the
same object rather than a drainpipe.

The styles are twelve silhouettes and surface treatments that read as
different objects on a shelf, not twelve sizes of the same one.
"""

from __future__ import annotations

from typing import Any

from .graph import Definition, Solution
from .mushroom import unit

TEMPLATE_ID = "vessel_vase"

# Every style is a set of slider positions. Diameters are written as they are
# meant to be seen at the style's own height; the proportion node rescales them
# when the height moves.
STYLES: dict[str, dict[str, Any]] = {
    "classic": {
        "height_mm": 180, "base_d_mm": 62, "mid_d_mm": 96, "mid_pos": 0.42,
        "neck_d_mm": 54, "neck_pos": 0.84, "rim_d_mm": 66, "shoulder": 0.5,
    },
    "amphora": {
        "height_mm": 200, "base_d_mm": 46, "mid_d_mm": 104, "mid_pos": 0.5,
        "neck_d_mm": 44, "neck_pos": 0.86, "rim_d_mm": 58, "shoulder": 0.6,
    },
    "bottle": {
        "height_mm": 210, "base_d_mm": 74, "mid_d_mm": 86, "mid_pos": 0.22,
        "neck_d_mm": 30, "neck_pos": 0.66, "rim_d_mm": 34, "shoulder": 0.85,
    },
    "bud": {
        "height_mm": 130, "base_d_mm": 36, "mid_d_mm": 48, "mid_pos": 0.22,
        "neck_d_mm": 22, "neck_pos": 0.7, "rim_d_mm": 26, "shoulder": 0.7,
        "wall_mm": 1.4,
    },
    "tulip": {
        "height_mm": 165, "base_d_mm": 42, "mid_d_mm": 54, "mid_pos": 0.28,
        "neck_d_mm": 70, "neck_pos": 0.78, "rim_d_mm": 106, "shoulder": 0.55,
        "rim_band_mm": 0.8,
    },
    "hourglass": {
        "height_mm": 175, "base_d_mm": 84, "mid_d_mm": 46, "mid_pos": 0.45,
        "neck_d_mm": 74, "neck_pos": 0.88, "rim_d_mm": 82, "shoulder": 0.65,
    },
    "cylinder": {
        "height_mm": 190, "base_d_mm": 78, "mid_d_mm": 76, "mid_pos": 0.45,
        "neck_d_mm": 76, "neck_pos": 0.85, "rim_d_mm": 80, "shoulder": 0.2,
        "rim_band_mm": 1.0,
    },
    "faceted": {
        "height_mm": 180, "base_d_mm": 56, "mid_d_mm": 92, "mid_pos": 0.38,
        "neck_d_mm": 62, "neck_pos": 0.82, "rim_d_mm": 74, "shoulder": 0.35,
        "facets": 6, "facet_round": 0.05,
    },
    "crystal": {
        "height_mm": 180, "base_d_mm": 50, "mid_d_mm": 94, "mid_pos": 0.5,
        "neck_d_mm": 58, "neck_pos": 0.84, "rim_d_mm": 66, "shoulder": 0.15,
        "facets": 5, "facet_round": 0.02, "twist_deg": 140,
    },
    "spiral": {
        "height_mm": 190, "base_d_mm": 60, "mid_d_mm": 92, "mid_pos": 0.45,
        "neck_d_mm": 70, "neck_pos": 0.85, "rim_d_mm": 78, "shoulder": 0.5,
        "lobes": 9, "lobe_mm": 3.4, "flute_sharp": 0.4, "twist_deg": 300,
    },
    "fluted": {
        "height_mm": 210, "base_d_mm": 78, "mid_d_mm": 82, "mid_pos": 0.5,
        "neck_d_mm": 78, "neck_pos": 0.85, "rim_d_mm": 88, "shoulder": 0.2,
        "lobes": 12, "lobe_mm": 2.2, "flute_sharp": 0.85, "rim_band_mm": 1.2,
    },
    "rippled": {
        "height_mm": 180, "base_d_mm": 64, "mid_d_mm": 92, "mid_pos": 0.44,
        "neck_d_mm": 74, "neck_pos": 0.86, "rim_d_mm": 80, "shoulder": 0.5,
        "ripples": 11, "ripple_mm": 2.2,
    },
    # --- the embossed nine ---------------------------------------------------
    # A silhouette and a garden on it. The band is stated in height fractions,
    # so it stays where it was put when the vase is scaled; the repeat count is
    # not, because a repeat is a flower and a flower is cast at the size a
    # flower is cast at.
    "vineyard": {
        "height_mm": 185, "base_d_mm": 62, "mid_d_mm": 98, "mid_pos": 0.42,
        "neck_d_mm": 56, "neck_pos": 0.84, "rim_d_mm": 68, "shoulder": 0.55,
        "emboss": "vine", "emboss_mm": 1.8, "emboss_count": 5, "emboss_rows": 3,
        "emboss_sharp": 0.5, "emboss_lo": 0.14, "emboss_hi": 0.82,
    },
    "laurelled": {
        "height_mm": 200, "base_d_mm": 48, "mid_d_mm": 104, "mid_pos": 0.5,
        "neck_d_mm": 46, "neck_pos": 0.86, "rim_d_mm": 60, "shoulder": 0.6,
        "emboss": "laurel", "emboss_mm": 1.6, "emboss_count": 6, "emboss_rows": 4,
        "emboss_sharp": 0.55, "emboss_lo": 0.2, "emboss_hi": 0.9,
    },
    "fernery": {
        "height_mm": 210, "base_d_mm": 70, "mid_d_mm": 76, "mid_pos": 0.4,
        "neck_d_mm": 72, "neck_pos": 0.86, "rim_d_mm": 78, "shoulder": 0.3,
        "emboss": "fern", "emboss_mm": 1.5, "emboss_count": 7, "emboss_rows": 5,
        "emboss_sharp": 0.45, "emboss_lo": 0.06, "emboss_hi": 0.96,
        "rim_band_mm": 0.8,
    },
    "posy": {
        "height_mm": 165, "base_d_mm": 44, "mid_d_mm": 56, "mid_pos": 0.28,
        "neck_d_mm": 70, "neck_pos": 0.78, "rim_d_mm": 104, "shoulder": 0.55,
        "emboss": "blossom", "emboss_mm": 2.0, "emboss_count": 6, "emboss_rows": 3,
        "emboss_sharp": 0.4, "emboss_lo": 0.1, "emboss_hi": 0.74,
        "rim_band_mm": 0.8,
    },
    "rosette": {
        "height_mm": 205, "base_d_mm": 76, "mid_d_mm": 88, "mid_pos": 0.24,
        "neck_d_mm": 32, "neck_pos": 0.68, "rim_d_mm": 36, "shoulder": 0.8,
        "emboss": "rose", "emboss_mm": 1.9, "emboss_count": 5, "emboss_rows": 2,
        "emboss_sharp": 0.35, "emboss_lo": 0.06, "emboss_hi": 0.5,
    },
    "fleur": {
        "height_mm": 180, "base_d_mm": 58, "mid_d_mm": 92, "mid_pos": 0.38,
        "neck_d_mm": 64, "neck_pos": 0.82, "rim_d_mm": 76, "shoulder": 0.4,
        "facets": 8, "facet_round": 0.55,
        "emboss": "lily", "emboss_mm": 1.7, "emboss_count": 8, "emboss_rows": 3,
        "emboss_sharp": 0.6, "emboss_lo": 0.16, "emboss_hi": 0.8,
    },
    "damascene": {
        "height_mm": 190, "base_d_mm": 78, "mid_d_mm": 80, "mid_pos": 0.45,
        "neck_d_mm": 78, "neck_pos": 0.85, "rim_d_mm": 82, "shoulder": 0.25,
        "emboss": "damask", "emboss_mm": 1.5, "emboss_count": 6, "emboss_rows": 5,
        "emboss_sharp": 0.6, "emboss_lo": 0.05, "emboss_hi": 0.95,
        "rim_band_mm": 1.0,
    },
    "trellised": {
        "height_mm": 185, "base_d_mm": 66, "mid_d_mm": 90, "mid_pos": 0.44,
        "neck_d_mm": 72, "neck_pos": 0.86, "rim_d_mm": 84, "shoulder": 0.5,
        "emboss": "trellis", "emboss_mm": 1.4, "emboss_count": 6, "emboss_rows": 5,
        "emboss_sharp": 0.7, "emboss_lo": 0.08, "emboss_hi": 0.92,
    },
    "artichoke": {
        "height_mm": 170, "base_d_mm": 58, "mid_d_mm": 110, "mid_pos": 0.5,
        "neck_d_mm": 66, "neck_pos": 0.86, "rim_d_mm": 74, "shoulder": 0.7,
        "emboss": "scale", "emboss_mm": 2.2, "emboss_count": 9, "emboss_rows": 7,
        "emboss_sharp": 0.5, "emboss_lo": 0.02, "emboss_hi": 0.94,
    },
}

STYLE_NOTE = {
    "classic": "a turned urn, the shape everyone pictures",
    "amphora": "high belly, narrow neck, small mouth",
    "bottle": "wide shoulders and a long throat",
    "bud": "small, for one stem",
    "tulip": "narrow foot opening into a wide mouth",
    "hourglass": "pinched at the waist",
    "cylinder": "straight-sided, banded at the rim",
    "faceted": "hexagonal cross-section, hard shoulders",
    "crystal": "pentagon with a slow twist",
    "spiral": "flutes wound most of a turn -- the vase-mode classic",
    "fluted": "a column of sharp ribs",
    "rippled": "horizontal rings up the wall",
    "vineyard": "an urn with a vine running round its belly",
    "laurelled": "a laurel spray up a tall amphora",
    "fernery": "ferns the whole way up a straight-sided column",
    "posy": "open blossoms under a wide tulip mouth",
    "rosette": "coiled roses round the shoulders of a bottle",
    "fleur": "lilies on eight soft facets",
    "damascene": "an ogee damask covering the wall",
    "trellised": "a leafy lattice, corner to corner",
    "artichoke": "imbricated petals, the way a pinecone is built",
}

# How far each slider may wander from its style. Tighter than the mushroom's:
# a vase's proportions are the design, and 20% on the belly is a different vase
# rather than a variation of this one.
JITTER: dict[str, tuple[str, float]] = {
    "height_mm": ("rel", 0.16),
    "mid_pos": ("abs", 0.05),
    "neck_pos": ("abs", 0.04),
    "shoulder": ("abs", 0.18),
    "facet_round": ("abs", 0.06),
    "lobe_mm": ("rel", 0.22),
    "flute_sharp": ("abs", 0.15),
    "twist_deg": ("rel", 0.3),
    "ripple_mm": ("rel", 0.25),
    "wall_mm": ("abs", 0.2),
    "rim_band_mm": ("abs", 0.4),
    "emboss_mm": ("rel", 0.18),
    "emboss_sharp": ("abs", 0.15),
    "emboss_lo": ("abs", 0.04),
    "emboss_hi": ("abs", 0.04),
}

# Diameters are not jittered on their own -- they are rebuilt from the style's
# own ratios against the height that came out of the jitter, then nudged.
PROPORTIONAL = ("base_d_mm", "mid_d_mm", "neck_d_mm", "rim_d_mm")
COUNTS = ("facets", "lobes", "ripples", "emboss_count", "emboss_rows")

DEFINITION = Definition("vase")

DEFINITION.slider(
    "style", "classic", choices=(*STYLES, "mixed"),
    doc="Which silhouette and surface to start from; `mixed` picks one per seed.",
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
    keys = (*JITTER, *PROPORTIONAL, *COUNTS, "style")
    return {key: unit(seed, "vase:" + key) for key in keys}


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
    """The expression box: rebuild the diameters from the style's own ratios.

    Every diameter is carried as a fraction of the style's height, so a vase
    that grew 16% taller grows in every direction at once. Jittering the four
    diameters independently is how you get a bottle with a rim wider than its
    shoulder -- valid, buildable, and not a bottle.
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
        wobble = 1.0 + 0.08 * variation * (draws[name] * 2.0 - 1.0)
        out[name] = height * ratio * wobble

    # Surface detail is counted, not scaled: a taller vase carries more rings
    # at the same spacing, and a wider one more flutes at the same pitch.
    if style.get("ripples") and "ripples" not in pinned:
        spacing = reference / max(style["ripples"], 1)
        out["ripples"] = max(3, round(height / spacing * (0.9 + 0.2 * draws["ripples"])))
    if style.get("lobes") and "lobes" not in pinned:
        pitch = style.get("mid_d_mm", 90) / max(style["lobes"], 1)
        out["lobes"] = max(4, round(out["mid_d_mm"] / pitch * (0.9 + 0.2 * draws["lobes"])))
    # A flower is cast at the size a flower is cast at. So the *count* follows
    # the vase -- more of them round a wider one, more rows up a taller band --
    # while the relief depth stays exactly where the style put it, because that
    # is a millimetre figure and the nozzle does not care how big the vase is.
    if out.get("emboss", "none") != "none":
        if "emboss_count" not in pinned and style.get("emboss_count"):
            pitch = style.get("mid_d_mm", 90) / max(style["emboss_count"], 1)
            out["emboss_count"] = max(
                3, round(out["mid_d_mm"] / pitch * (0.92 + 0.16 * draws["emboss_count"]))
            )
        if "emboss_rows" not in pinned and style.get("emboss_rows"):
            span = (style.get("emboss_hi", 0.9) - style.get("emboss_lo", 0.1)) * reference
            pitch = span / max(style["emboss_rows"], 1)
            band = (out["emboss_hi"] - out["emboss_lo"]) * height
            out["emboss_rows"] = max(
                1, round(band / max(pitch, 1e-6) * (0.92 + 0.16 * draws["emboss_rows"]))
            )
    return out


@DEFINITION.component("proportioned", name="feasible")
def _feasible(params: dict[str, Any]) -> dict[str, Any]:
    """Satisfy the template's preconditions, and the CPU budget behind them."""
    out = dict(params)

    # The neck sits above the belly, and both stay off the ends.
    out["mid_pos"] = min(max(out["mid_pos"], 0.16), 0.66)
    out["neck_pos"] = min(max(out["neck_pos"], out["mid_pos"] + 0.08), 0.95)
    # The cavity has to fit inside the narrowest diameter.
    floor = out["wall_mm"] * 2 + 7.0
    for name in PROPORTIONAL:
        out[name] = max(out[name], floor)
    # No segment of the silhouette turns faster than 45 degrees, in either
    # direction: flaring out that fast is an overhang, closing in that fast is
    # a bridge across the mouth, and the whole appeal of a vase is that it
    # needs neither. Walked bottom-up, so each fix is measured against the
    # segment below it that has already been fixed.
    height = out["height_mm"]
    spans = (
        ("base_d_mm", "mid_d_mm", out["mid_pos"]),
        ("mid_d_mm", "neck_d_mm", out["neck_pos"] - out["mid_pos"]),
        ("neck_d_mm", "rim_d_mm", 1.0 - out["neck_pos"]),
    )
    for lower, upper, span in spans:
        room = span * height * 2.0 * 0.96
        delta = out[upper] - out[lower]
        if abs(delta) > room:
            out[upper] = out[lower] + (room if delta > 0 else -room)
        out[upper] = max(out[upper], floor)

    # A flute is cut from both sides of the wall, so a deep one needs a neck
    # wide enough to have something left in the middle.
    if out["lobes"] >= 1:
        room = out["neck_d_mm"] / 2 - out["wall_mm"] * 2 - 3.5
        out["lobe_mm"] = max(0.0, min(out["lobe_mm"], room))
        if out["lobe_mm"] < 0.4:
            out["lobes"] = 0
    # Twist times detail is what the geometry cannot afford: the bands have to
    # stay a fraction of a flute apart. Losing some twist costs the least.
    detail = max(out["lobes"], out["facets"], 1)
    if abs(out["twist_deg"]) * detail > 3400:
        out["twist_deg"] = round(3400 / detail) * (1 if out["twist_deg"] >= 0 else -1)
    out["base_mm"] = min(out["base_mm"], out["height_mm"] * 0.25)

    # --- the relief -------------------------------------------------------
    if out.get("emboss", "none") != "none" and out["emboss_mm"] > 0.02:
        out["emboss_sharp"] = min(max(out["emboss_sharp"], 0.0), 1.0)
        out["emboss_mm"] = min(max(out["emboss_mm"], 0.2), 3.0)
        out["emboss_lo"] = min(max(out["emboss_lo"], 0.0), 0.85)
        out["emboss_hi"] = min(max(out["emboss_hi"], out["emboss_lo"] + 0.14), 1.0)
        if out["emboss_hi"] - out["emboss_lo"] < 0.14:
            out["emboss_lo"] = max(0.0, out["emboss_hi"] - 0.14)
        # A row needs enough height that the flower in it is a shape rather than
        # a scratch. The rows give way, not the band: where the ornament sits is
        # the design, how many times it repeats up it is not.
        band = (out["emboss_hi"] - out["emboss_lo"]) * height
        out["emboss_rows"] = int(min(max(out["emboss_rows"], 1), max(1, band // 10)))
        # And a repeat needs enough of the circumference to draw a flower in at
        # the narrowest place the band can reach.
        narrow = min(out[name] for name in PROPORTIONAL)
        while out["emboss_count"] > 1 and 3.14159 * narrow / out["emboss_count"] < 7.0:
            out["emboss_count"] -= 1
        out["emboss_count"] = int(min(max(out["emboss_count"], 1), 9))
        # A band that also winds round the vase needs bands per fraction of a
        # turn on top of bands per fraction of a row, and the two multiply.
        if abs(out["twist_deg"]) > 340:
            out["twist_deg"] = 340.0 if out["twist_deg"] > 0 else -340.0
    else:
        out["emboss"] = "none"
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
    style: str = "classic",
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
    style: str = "classic",
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
    return int(unit(seed, f"vase:member:{index}") * 10000)


def describe(params: dict[str, Any]) -> str:
    """A one-line label: how big it is and what it wears."""
    surface = []
    if params.get("facets", 0) >= 3:
        surface.append(f"{params['facets']} facets")
    if params.get("lobes", 0) >= 1 and params.get("lobe_mm", 0) > 0:
        surface.append(f"{params['lobes']} flutes")
    if params.get("ripples", 0) >= 1 and params.get("ripple_mm", 0) > 0:
        surface.append(f"{params['ripples']} rings")
    if abs(params.get("twist_deg", 0)) >= 15:
        surface.append(f"{params['twist_deg']:.0f}° twist")
    if params.get("emboss", "none") != "none" and params.get("emboss_mm", 0) > 0.02:
        surface.append(
            f"{params['emboss']} relief {params['emboss_mm']:.1f} mm proud"
            f" x{params.get('emboss_count', 0)}"
        )
    widest = max(params.get(k, 0) for k in PROPORTIONAL)
    return (
        f"{params.get('height_mm', 0):.0f} mm tall, {widest:.0f} mm across, "
        f"{params.get('wall_mm', 0):.1f} mm wall"
        + (", " + " + ".join(surface) if surface else ", plain")
    )
