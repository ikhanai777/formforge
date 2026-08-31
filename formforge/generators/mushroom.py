"""The mushroom definition: a species and a seed in, a parameter set out.

This is the Grasshopper half of the mushroom generator. The geometry lives in
the `nature_mushroom` template, which is the equivalent of the components on the
canvas that actually make surfaces; this module is the part of a definition
that sits to the left of them -- the sliders, the value list, the random
component, and the expression boxes that keep the numbers in proportion to each
other as the sliders move.

The pipeline, in solve order:

    species -----> preset ------\\
    seed --------> draws --------> jittered --> proportioned --> feasible --> params
    variation ------------------/

* **preset** is the value list: seven species, each a set of slider positions.
* **draws** is the random component: a seed in, a stable stream of unit numbers
  out, keyed by parameter name so that adding a parameter never reshuffles the
  ones already there.
* **jittered** moves the free sliders off the preset by up to their own range,
  scaled by how much variation was asked for.
* **proportioned** is the expression box. A mushroom whose cap grew 16% and
  whose stem shrank 18% is not a variation, it is a different mushroom badly
  drawn: the dependent sliders are recomputed from the preset's own ratios
  against the cap that came out of the jitter, not jittered on their own.
* **feasible** enforces the template's preconditions, so a variation is never
  rejected by the geometry it was built for.
* **params** clamps to the template schema -- the single authority on what the
  geometry has actually been built and swept across -- and rounds.

Same seed, same mushroom: everything here is a pure function of the inputs, and
the scatter inside the geometry is driven by the same seed value.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .graph import Definition, Solution

TEMPLATE_ID = "nature_mushroom"

# Each species is a set of slider positions, not a separate model: the whole
# genus is one definition. Values that are absent take the template's default.
SPECIES: dict[str, dict[str, Any]] = {
    "toadstool": {
        "cap_d_mm": 62, "cap_h_mm": 26, "cap_fullness": 2.2, "cap_shoulder": 0.62,
        "cap_umbo_mm": 2.5, "cap_flesh_mm": 6.0, "cap_lobes": 5, "cap_wave_mm": 2.0,
        "underside": "gills", "stem_h_mm": 58, "stem_d_mm": 14, "stem_taper": 0.2,
        "stem_bulb": 0.5, "stem_lean_mm": 6, "ring_style": "skirt", "wart_count": 22,
    },
    "fly_agaric": {
        "cap_d_mm": 66, "cap_h_mm": 30, "cap_fullness": 2.4, "cap_shoulder": 0.55,
        "cap_umbo_mm": 0.0, "cap_flesh_mm": 6.5, "cap_lobes": 4, "cap_wave_mm": 1.5,
        "underside": "gills", "stem_h_mm": 72, "stem_d_mm": 13, "stem_taper": 0.22,
        "stem_bulb": 0.75, "stem_lean_mm": 5, "ring_style": "skirt", "wart_count": 24,
        "wart_flatten": 0.5,
    },
    "parasol": {
        "cap_d_mm": 84, "cap_h_mm": 18, "cap_fullness": 3.6, "cap_shoulder": 0.5,
        "cap_umbo_mm": 6.0, "cap_flesh_mm": 4.5, "cap_lobes": 7, "cap_wave_mm": 2.5,
        "underside": "gills", "stem_h_mm": 92, "stem_d_mm": 10, "stem_taper": 0.15,
        "stem_bulb": 0.9, "stem_lean_mm": 7, "ring_style": "band", "wart_count": 26,
        "wart_flatten": 0.3,
    },
    "bolete": {
        "cap_d_mm": 74, "cap_h_mm": 30, "cap_fullness": 3.0, "cap_shoulder": 0.42,
        "cap_umbo_mm": 0.0, "cap_flesh_mm": 9.0, "cap_lobes": 3, "cap_wave_mm": 1.2,
        "underside": "pores", "stem_h_mm": 48, "stem_d_mm": 26, "stem_taper": -0.15,
        "stem_bulb": 0.35, "stem_lean_mm": 3, "ring_style": "none", "wart_count": 0,
    },
    "chanterelle": {
        "cap_d_mm": 52, "cap_h_mm": 15, "cap_fullness": 1.6, "cap_shoulder": 0.8,
        "cap_dish_mm": 11, "cap_umbo_mm": 0.0, "cap_flesh_mm": 5.0, "cap_lobes": 6,
        "cap_wave_mm": 5.0, "cap_tilt_deg": 7, "underside": "gills", "gill_t_mm": 1.8,
        "stem_h_mm": 42, "stem_d_mm": 16, "stem_taper": 0.35, "stem_bulb": 0.1,
        "stem_lean_mm": 8, "ring_style": "none", "wart_count": 0,
    },
    "ink_cap": {
        "cap_d_mm": 34, "cap_h_mm": 34, "cap_fullness": 1.3, "cap_shoulder": 0.95,
        "cap_umbo_mm": 0.0, "cap_flesh_mm": 3.5, "cap_margin_mm": 1.0, "cap_lobes": 8,
        "cap_wave_mm": 1.5, "underside": "gills", "gill_t_mm": 0.9, "stem_h_mm": 86,
        "stem_d_mm": 8, "stem_taper": 0.1, "stem_bulb": 0.3, "stem_lean_mm": 9,
        "ring_style": "none", "wart_count": 0,
    },
    "button": {
        "cap_d_mm": 40, "cap_h_mm": 22, "cap_fullness": 2.0, "cap_shoulder": 0.5,
        "cap_umbo_mm": 1.0, "cap_flesh_mm": 7.0, "cap_lobes": 0, "cap_wave_mm": 0.0,
        "underside": "gills", "stem_h_mm": 34, "stem_d_mm": 16, "stem_taper": 0.12,
        "stem_bulb": 0.25, "stem_lean_mm": 2, "ring_style": "band", "wart_count": 0,
    },
}

# Which sliders are free to wander, and how far at variation = 1.0.
# ("rel", f) moves by up to +/- f of the value; ("abs", d) by up to +/- d.
JITTER: dict[str, tuple[str, float]] = {
    "cap_d_mm": ("rel", 0.16),
    "cap_h_mm": ("rel", 0.22),
    "cap_fullness": ("rel", 0.20),
    "cap_shoulder": ("rel", 0.15),
    "cap_dish_mm": ("rel", 0.40),
    "cap_umbo_mm": ("abs", 1.6),
    "cap_flesh_mm": ("rel", 0.18),
    "cap_margin_mm": ("rel", 0.15),
    "cap_lobes": ("abs", 2.0),
    "cap_wave_mm": ("rel", 0.45),
    "cap_tilt_deg": ("abs", 3.0),
    "gill_t_mm": ("rel", 0.15),
    "stem_taper": ("abs", 0.08),
    "stem_bulb": ("abs", 0.22),
    "stem_lean_mm": ("rel", 0.70),
    "stem_flare": ("abs", 0.15),
    "ring_pos": ("abs", 0.07),
    "wart_flatten": ("abs", 0.12),
}

# Sliders the proportion node computes rather than jitters, and the ratio it
# computes them from. A mushroom reads as one organism because its parts scale
# together; jittering these independently is what makes generated variations
# look like a parts bin.
DERIVED = ("stem_h_mm", "stem_d_mm", "gill_count", "gill_depth_mm", "wart_count",
           "wart_d_mm", "ring_w_mm")

CATEGORICAL = ("underside", "ring_style")

DEFINITION = Definition("mushroom")

DEFINITION.slider(
    "species", "toadstool", choices=(*SPECIES, "mixed"),
    doc="Which set of slider positions to start from; `mixed` picks one per seed.",
)
DEFINITION.slider(
    "seed", 7, low=0, high=9999, doc="Drives the jitter and the scatter in the geometry."
)
DEFINITION.slider(
    "variation", 0.55, low=0.0, high=1.0,
    doc="How far a specimen may wander from its species. 0 rebuilds the preset exactly.",
)
DEFINITION.slider(
    "overrides",
    {},
    doc="Slider values pinned by the caller; honoured unless the geometry cannot take them.",
)


@DEFINITION.component(name="bounds")
def _bounds() -> dict[str, dict[str, Any]]:
    """The template schema: the authority on what the geometry accepts.

    Read from the registry rather than restated here, so the ranges the
    generator samples are the ranges the template was actually swept across.
    """
    return schema_bounds()


@DEFINITION.component("species", "seed", "bounds", "overrides", name="preset")
def _preset(
    species: str, seed: int, bounds: dict[str, dict[str, Any]], overrides: dict[str, Any]
) -> dict[str, Any]:
    """The value list: slider positions for one species, over the defaults.

    A caller's pins land here rather than at the end, so everything downstream
    derives from them: pin a 90 mm cap and the stem, the gills and the warts are
    sized for a 90 mm cap.
    """
    if species == "mixed":
        names = tuple(SPECIES)
        species = names[int(unit(seed, "species") * len(names)) % len(names)]
    base = {name: spec.get("default") for name, spec in bounds.items()}
    base.update(SPECIES[species])
    base.update({k: v for k, v in (overrides or {}).items() if k in bounds})
    base["species"] = species
    return base


@DEFINITION.component("seed", name="draws")
def _draws(seed: int) -> dict[str, float]:
    """The random component: one stable unit draw per parameter name.

    Keyed by name rather than by position, so adding a slider later does not
    reshuffle every mushroom that came before it.
    """
    keys = (*JITTER, *DERIVED, "species", "wobble")
    return {key: unit(seed, key) for key in keys}


@DEFINITION.component("preset", "draws", "variation", "overrides", name="jittered")
def _jittered(
    preset: dict[str, Any],
    draws: dict[str, float],
    variation: float,
    overrides: dict[str, Any],
) -> dict[str, Any]:
    """Move the free sliders off the preset, by up to their own jitter range."""
    out = dict(preset)
    pinned = set(overrides or ())
    for name, (mode, amount) in JITTER.items():
        value = preset.get(name)
        if name in pinned or not isinstance(value, (int, float)):
            continue
        swing = (draws[name] * 2.0 - 1.0) * variation
        moved = value * (1.0 + amount * swing) if mode == "rel" else value + amount * swing
        out[name] = max(0.0, moved)
    out["cap_lobes"] = round(out["cap_lobes"])
    return out


@DEFINITION.component("preset", "jittered", "draws", "overrides", name="proportioned")
def _proportioned(
    preset: dict[str, Any],
    jittered: dict[str, Any],
    draws: dict[str, float],
    overrides: dict[str, Any],
) -> dict[str, Any]:
    """The expression box: rebuild the dependent sliders from the preset's ratios.

    Everything here is a function of the cap that came out of the jitter, so a
    specimen that grew a wider cap gets the stem, the gill count and the wart
    size that go with it.
    """
    out = dict(jittered)
    pinned = set(overrides or ())
    cap_d = out["cap_d_mm"]
    # Ratios come from the species itself, never from the preset a pin has
    # already been folded into: pinning a 100 mm cap has to make the stem grow
    # with it, and it cannot if the reference grew too.
    species = SPECIES[preset["species"]]
    scale = cap_d / max(species.get("cap_d_mm", preset["cap_d_mm"]), 1e-6)

    def wobble(key: str, spread: float) -> float:
        return 1.0 + spread * (draws[key] * 2.0 - 1.0)

    derived: dict[str, Any] = {}
    derived["stem_h_mm"] = species["stem_h_mm"] * scale * wobble("stem_h_mm", 0.18)
    derived["stem_d_mm"] = species["stem_d_mm"] * scale * wobble("stem_d_mm", 0.12)
    # Gill count follows the circumference, but every blade is a solid in the
    # union and a bigger cap makes every one of those booleans dearer, so the
    # count is also capped by what the sandbox's CPU ceiling will take. A 90 mm
    # cap gets coarser gills than a 60 mm one; it also builds inside 30 seconds.
    derived["gill_count"] = round(
        min(cap_d * 0.26, 1000.0 / max(cap_d, 1.0)) * wobble("gill_count", 0.15)
    )
    derived["gill_depth_mm"] = min(out["cap_flesh_mm"] + 2.0, cap_d * 0.11) * wobble(
        "gill_depth_mm", 0.15
    )
    derived["ring_w_mm"] = derived["stem_d_mm"] * 0.42 * wobble("ring_w_mm", 0.2)
    # A species with a bare cap keeps it bare however the seed falls.
    if species.get("wart_count", 0):
        derived["wart_count"] = round(
            min(cap_d * cap_d * 0.0040, 1600.0 / max(cap_d, 1.0))
            * wobble("wart_count", 0.3)
        )
        derived["wart_d_mm"] = cap_d * 0.068 * wobble("wart_d_mm", 0.2)
    else:
        derived["wart_count"] = 0

    out.update({k: v for k, v in derived.items() if k not in pinned})
    return out


@DEFINITION.component("proportioned", name="feasible")
def _feasible(params: dict[str, Any]) -> dict[str, Any]:
    """Satisfy the template's preconditions.

    The template rejects parameter combinations it cannot build, which is the
    right behaviour for a schema and the wrong outcome for a generator: a
    variation that gets refused is a variation nobody sees. Every rule the
    template states as a precondition is enforced here instead, by moving the
    parameter that costs the mushroom least. Feasibility outranks a caller's
    pin -- a pinned value that the geometry cannot build is moved, and the
    solution shows what it was moved to.
    """
    out = dict(params)

    # The cap needs more height than it has flesh, or the underside is above the
    # crown.
    out["cap_h_mm"] = max(out["cap_h_mm"], out["cap_flesh_mm"] + out["cap_margin_mm"] + 1.0)
    # The stem carries the cap; it has to be longer than the cap is deep.
    out["stem_h_mm"] = max(out["stem_h_mm"], out["cap_h_mm"] * 0.5 + 13.0)
    # The stem has to fit under the cap, and leave an annulus for the gills.
    out["stem_d_mm"] = min(out["stem_d_mm"], out["cap_d_mm"] * 0.55)
    if out["underside"] != "smooth":
        room = out["cap_d_mm"] / 2 - out["cap_margin_mm"] * 1.5 - out["cap_wave_mm"]
        out["stem_d_mm"] = min(out["stem_d_mm"], (room - 7.0 - 1.5) / 0.625)
    if out["underside"] == "gills":
        out["gill_depth_mm"] = min(out["gill_depth_mm"], out["cap_flesh_mm"] + 2.5)
    # The ring is clamped below the cap, so there has to be stem below the cap.
    if out["ring_style"] != "none":
        needed = out["cap_h_mm"] - out["cap_flesh_mm"] * 0.7 + out["ring_w_mm"] + 6.0
        out["stem_h_mm"] = max(out["stem_h_mm"], needed)
    return out


@DEFINITION.component("feasible", "bounds", name="params")
def _params(params: dict[str, Any], bounds: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Clamp to the template schema and round to sensible slider positions."""
    return {
        name: _fit(params.get(name, spec.get("default")), spec) for name, spec in bounds.items()
    }


def _fit(value: Any, spec: dict[str, Any]) -> Any:
    """One value, coerced and clamped into what the schema declares."""
    kind = spec.get("type")
    choices = spec.get("enum")
    if choices:
        return value if value in choices else spec.get("default", choices[0])
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
    return round(float(value), 2)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def species_names() -> tuple[str, ...]:
    return tuple(SPECIES)


def unit(seed: int, key: str) -> float:
    """A stable draw in [0, 1) from a seed and a name.

    Not `random`: the stream has to be identical across processes and Python
    versions, because the seed is the only record of how a mushroom was made.
    """
    digest = hashlib.blake2b(f"{int(seed)}|{key}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2**64


def schema_bounds(template: Any = None) -> dict[str, dict[str, Any]]:
    """The template's parameter schema, loaded once."""
    global _BOUNDS
    if template is not None:
        return dict(template.properties)
    if _BOUNDS is None:
        from ..registry import TemplateRegistry  # noqa: PLC0415

        _BOUNDS = dict(TemplateRegistry.load(strict=False).get(TEMPLATE_ID).properties)
    return _BOUNDS


_BOUNDS: dict[str, dict[str, Any]] | None = None


def solve(
    seed: int = 7,
    *,
    species: str = "toadstool",
    variation: float = 0.55,
    overrides: dict[str, Any] | None = None,
) -> Solution:
    """Run the definition. The solution carries every intermediate value."""
    if species not in SPECIES and species != "mixed":
        raise ValueError(
            f"unknown species {species!r}; known: {', '.join(SPECIES)}, mixed"
        )
    solution = DEFINITION.solve(
        species=species, seed=int(seed), variation=variation, overrides=overrides or {}
    )
    solution.values["params"]["seed"] = int(seed) % 10000
    return solution


def specimen(
    seed: int = 7,
    *,
    species: str = "toadstool",
    variation: float = 0.55,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One mushroom's parameters. Same arguments, same mushroom."""
    solution = solve(seed, species=species, variation=variation, overrides=overrides)
    return dict(solution["params"])


def variations(
    count: int = 6,
    *,
    seed: int = 7,
    species: str = "mixed",
    variation: float = 0.55,
    overrides: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """A population of distinct mushrooms from one seed.

    Member k is solved with its own derived seed rather than with a running
    generator, so `variations(10)[3]` is the same mushroom as `variations(4)[3]`
    -- a population you can extend without renumbering what you already printed.
    """
    return [
        specimen(
            member_seed(seed, index), species=species, variation=variation, overrides=overrides
        )
        for index in range(max(0, count))
    ]


def member_seed(seed: int, index: int) -> int:
    """The seed for member `index` of the population grown from `seed`."""
    return int(unit(seed, f"member:{index}") * 10000)


def describe(params: dict[str, Any]) -> str:
    """A one-line label for a parameter set: what it is and how big."""
    height = params.get("stem_h_mm", 0) + params.get("cap_flesh_mm", 0)
    return (
        f"{params.get('cap_d_mm', 0):.0f} mm cap, {height:.0f} mm tall, "
        f"{params.get('underside', 'gills')}, "
        f"{params.get('wart_count', 0)} warts, ring {params.get('ring_style', 'none')}"
    )
