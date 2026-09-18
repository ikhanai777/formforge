"""The pattern catalogue.

Each entry is a *kernel factory*: given its parameters, the panel bounds and a
seed, it returns a pure function of world (x, y). Everything else in this
package -- tiling, joinery, meshing -- is generic over that function, so adding
a pattern is adding one function and one parameter table, and it immediately
tiles, joins and prints like every other pattern.

Three rules hold for every kernel here, and breaking any of them breaks the
seams:

1. **Pure and global.** No per-tile state, no RNG, no dependence on which
   window is being sampled. `f(x, y)` is the same number wherever it is asked
   from.
2. **Vectorised.** It is handed two float arrays of matching shape and returns
   one.
3. **Finite.** NaN and infinity are handled defensively downstream, but a
   kernel that produces them routinely will produce visible artifacts where
   they were clamped.

The parameter tables are not just validation. They are what `formforge patterns
<id>` prints, and the ranges are chosen to be the range in which the pattern
*prints well*, not the range in which the maths is defined -- a 2 mm wave
period is perfectly good mathematics and an unresolvable smudge on a 0.4 mm
nozzle.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from . import noise
from .field import Bounds, HeightFn, polar, rotate

__all__ = [
    "FAMILIES",
    "PATTERNS",
    "Param",
    "PatternSpec",
    "build_field_fn",
    "get_pattern",
    "resolve_params",
]

FAMILIES = {
    "water": "Waves, swell, ripples -- anything with a travelling crest",
    "landscape": "Terrain: dunes, hills, ridges, erosion, contour maps",
    "mathematical": "Fields with a closed form worth knowing about",
    "tessellation": "Repeating cells: Voronoi, Truchet, hex, woven",
}


@dataclass(frozen=True)
class Param:
    """One knob, with the range in which it prints well."""

    name: str
    kind: str  # number | integer | choice | boolean
    default: Any
    description: str
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] | None = None
    unit: str = ""

    def coerce(self, raw: Any) -> Any:
        """Parse and range-check one value, with an error that says what to do."""
        if self.kind == "choice":
            value = str(raw)
            if value not in (self.choices or ()):
                raise ValueError(
                    f"{self.name}={value!r} is not one of {', '.join(self.choices or ())}"
                )
            return value
        if self.kind == "boolean":
            if isinstance(raw, bool):
                return raw
            value = str(raw).strip().lower()
            if value in {"1", "true", "yes", "on"}:
                return True
            if value in {"0", "false", "no", "off"}:
                return False
            raise ValueError(f"{self.name}={raw!r} is not a true/false value")

        try:
            number = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{self.name}={raw!r} is not a number") from None
        if not math.isfinite(number):
            raise ValueError(f"{self.name}={raw!r} is not a finite number")
        if self.minimum is not None and number < self.minimum:
            raise ValueError(
                f"{self.name}={_fmt(number)} is below the minimum "
                f"{_fmt(self.minimum)}{self.unit}"
            )
        if self.maximum is not None and number > self.maximum:
            raise ValueError(
                f"{self.name}={_fmt(number)} is above the maximum "
                f"{_fmt(self.maximum)}{self.unit}"
            )
        return round(number) if self.kind == "integer" else number

    def describe_range(self) -> str:
        if self.kind == "choice":
            return " | ".join(self.choices or ())
        if self.kind == "boolean":
            return "true | false"
        lo = _fmt(self.minimum) if self.minimum is not None else "-inf"
        hi = _fmt(self.maximum) if self.maximum is not None else "inf"
        return f"{lo} .. {hi}{self.unit}"


def _fmt(value: float | None) -> str:
    if value is None:
        return "-"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


KernelFactory = Callable[[dict[str, Any], Bounds, int], HeightFn]


@dataclass(frozen=True)
class PatternSpec:
    """A pattern: what it is, what it takes, and how it prints."""

    id: str
    family: str
    display_name: str
    description: str
    kernel: KernelFactory
    params: tuple[Param, ...] = ()
    tags: tuple[str, ...] = ()
    print_note: str = ""
    # Relief depth, in mm, that suits this pattern on a wall panel. Used as the
    # default when the caller does not say; a Truchet ribbon at 12 mm is a trip
    # hazard and a dune field at 1.5 mm is a texture, not a landscape.
    suggested_relief_mm: float = 5.0

    def defaults(self) -> dict[str, Any]:
        return {p.name: p.default for p in self.params}

    def param(self, name: str) -> Param:
        for p in self.params:
            if p.name == name:
                return p
        raise KeyError(name)


def resolve_params(
    spec: PatternSpec, overrides: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Merge overrides onto defaults, coercing and range-checking each one."""
    values = spec.defaults()
    known = {p.name for p in spec.params}
    for name, raw in (overrides or {}).items():
        if name not in known:
            suggestion = _closest(name, known)
            hint = f"; did you mean {suggestion}?" if suggestion else ""
            raise ValueError(
                f"{spec.id} has no parameter {name!r}{hint}. "
                f"Parameters: {', '.join(sorted(known)) or '(none)'}"
            )
        values[name] = spec.param(name).coerce(raw)
    return values


def _closest(name: str, candidates: set[str]) -> str | None:
    """Cheap nearest-name suggestion -- shared prefix beats nothing at all."""
    best, best_score = None, 0
    for candidate in candidates:
        score = len(_common_prefix(name, candidate))
        if score > best_score:
            best, best_score = candidate, score
    return best if best_score >= 3 else None


def _common_prefix(a: str, b: str) -> str:
    out = []
    for ca, cb in zip(a, b, strict=False):
        if ca != cb:
            break
        out.append(ca)
    return "".join(out)


# --------------------------------------------------------------------------
# Shared parameter fragments
# --------------------------------------------------------------------------


def _angle(default: float = 0.0, description: str = "Rotation of the whole pattern.") -> Param:
    return Param("angle_deg", "number", default, description, 0.0, 360.0, unit=" deg")


def _scale(default: float, lo: float, hi: float, description: str) -> Param:
    return Param("scale_mm", "number", default, description, lo, hi, unit=" mm")


def _octaves(default: int = 4) -> Param:
    return Param(
        "octaves",
        "integer",
        default,
        "Levels of detail. Each one halves the feature size; past the point "
        "where a feature is narrower than the nozzle it only adds triangles.",
        1,
        8,
    )


# --------------------------------------------------------------------------
# Water
# --------------------------------------------------------------------------


def _waves(p: dict, bounds: Bounds, seed: int) -> HeightFn:
    wavelength = max(p["wavelength_mm"], 1e-6)
    crest = p["crest"]
    harmonic = p["harmonic"]
    meander = p["meander_mm"]
    angle = p["angle_deg"]
    center = bounds.center

    def fn(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        rx, ry = rotate(x, y, angle, center)
        phase = 2.0 * np.pi * rx / wavelength
        if meander > 0.0:
            # Bend the crest lines along their own length. Perfectly straight
            # wave trains read as corrugation; a crest that wanders by a few
            # millimetres over a metre reads as water.
            phase = phase + noise.fbm(
                rx / (wavelength * 6), ry / (wavelength * 6), octaves=2, seed=seed
            ) * (2.0 * np.pi * meander / wavelength)
        # Skewing the phase by its own sine steepens the front of each crest and
        # stretches the trough -- the cheap standing-in for a Gerstner wave, and
        # the thing that stops a sine looking like a sine.
        skewed = phase + crest * np.sin(phase)
        h = np.sin(skewed)
        if harmonic > 0.0:
            h = h + harmonic * np.sin(2.0 * skewed + 1.1)
        return h

    return fn


def _ocean(p: dict, bounds: Bounds, seed: int) -> HeightFn:
    trains = int(p["trains"])
    base_wl = max(p["swell_mm"], 1e-6)
    spread = math.radians(p["spread_deg"])
    chop = p["chop"]
    heading = math.radians(p["angle_deg"])

    # Precompute each train once: direction, wavenumber, amplitude, phase. A
    # real sea is a spectrum, and the thing that makes it look like one rather
    # than like three sine waves is that the short waves are both steeper and
    # weaker than the long ones.
    components = []
    for i in range(trains):
        frac = i / max(trains - 1, 1)
        theta = heading + (frac - 0.5) * 2.0 * spread
        wl = base_wl * (1.0 - 0.62 * frac)
        amp = (1.0 - 0.55 * frac) ** 2
        phase = noise.hash_unit(i, seed) * 2.0 * math.pi
        components.append((math.cos(theta), math.sin(theta), 2.0 * np.pi / wl, amp, phase))

    def fn(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        total = np.zeros_like(x, dtype=np.float64)
        for cx, sy, k, amp, phi in components:
            phase = (x * cx + y * sy) * k + phi
            total += amp * np.sin(phase + chop * np.sin(phase))
        if chop > 0.0:
            total += (
                chop
                * 0.35
                * noise.fbm(
                    x / (base_wl * 0.22), y / (base_wl * 0.22), octaves=3, seed=seed + 31
                )
            )
        return total

    return fn


def _ripples(p: dict, bounds: Bounds, seed: int) -> HeightFn:
    wavelength = max(p["wavelength_mm"], 1e-6)
    decay = max(p["decay_mm"], 1e-6)
    sources = int(p["sources"])
    spread = p["spread_mm"]

    cx, cy = bounds.center
    points = [(cx, cy)]
    for i in range(1, sources):
        theta = noise.hash_unit(i * 3, seed) * 2.0 * math.pi
        # sqrt of a uniform spreads the drops evenly over the disc rather than
        # bunching them near the middle.
        r = spread * math.sqrt(0.15 + 0.85 * noise.hash_unit(i * 3 + 1, seed))
        points.append((cx + r * math.cos(theta), cy + r * math.sin(theta)))
    phases = [noise.hash_unit(i * 3 + 2, seed) * 2.0 * math.pi for i in range(sources)]

    def fn(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        total = np.zeros_like(x, dtype=np.float64)
        for (px, py), phi in zip(points, phases, strict=True):
            r = np.hypot(x - px, y - py)
            # Amplitude falls off with distance, as a dropped-stone ring does.
            total += np.cos(2.0 * np.pi * r / wavelength + phi) * np.exp(-r / decay)
        return total

    return fn


def _interference(p: dict, bounds: Bounds, seed: int) -> HeightFn:
    sources = int(p["sources"])
    wavelength = max(p["wavelength_mm"], 1e-6)
    radius = p["radius_mm"]
    angle = math.radians(p["angle_deg"])
    cx, cy = bounds.center
    points = [
        (
            cx + radius * math.cos(angle + 2 * math.pi * i / sources),
            cy + radius * math.sin(angle + 2 * math.pi * i / sources),
        )
        for i in range(sources)
    ]

    def fn(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        total = np.zeros_like(x, dtype=np.float64)
        for px, py in points:
            total += np.cos(2.0 * np.pi * np.hypot(x - px, y - py) / wavelength)
        return total

    return fn


def _sand_ripples(p: dict, bounds: Bounds, seed: int) -> HeightFn:
    ripple = max(p["ripple_mm"], 1e-6)
    cross = p["cross"]
    wander = p["wander"]
    angle = p["angle_deg"]
    center = bounds.center

    def fn(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        rx, ry = rotate(x, y, angle, center)
        if wander > 0.0:
            drift = (
                noise.fbm(rx / (ripple * 14), ry / (ripple * 14), octaves=3, seed=seed)
                * ripple
                * 3.0
                * wander
            )
            rx = rx + drift
        primary = np.sin(2.0 * np.pi * rx / ripple)
        # Wind ripples are not a pure corrugation: the crests break and restart,
        # which a weak transverse set reproduces well.
        secondary = np.sin(2.0 * np.pi * ry / (ripple * 5.0) + 0.6)
        h = primary * (0.75 + 0.25 * secondary)
        if cross > 0.0:
            h = h + cross * np.sin(2.0 * np.pi * (rx * 0.35 + ry) / (ripple * 1.7))
        return h + 0.12 * noise.fbm(
            rx / (ripple * 0.9), ry / (ripple * 0.9), octaves=2, seed=seed + 5
        )

    return fn


# --------------------------------------------------------------------------
# Landscape
# --------------------------------------------------------------------------


def _dunes(p: dict, bounds: Bounds, seed: int) -> HeightFn:
    spacing = max(p["dune_mm"], 1e-6)
    slip = float(np.clip(p["slip"], 0.05, 0.45))
    meander = p["meander"]
    ripples = p["ripples"]
    angle = p["angle_deg"]
    center = bounds.center

    def fn(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        rx, ry = rotate(x, y, angle, center)
        # The crest line of a dune is not straight and the dunes are not evenly
        # spaced; both come from one low-frequency field so they stay correlated.
        drift = (
            noise.fbm(rx / (spacing * 2.2), ry / (spacing * 1.1), octaves=3, seed=seed)
            * spacing
            * meander
        )
        phase = (rx + drift) / spacing
        frac = phase - np.floor(phase)

        # Asymmetric profile: a long windward rise, then a short steep slip
        # face. That asymmetry is the whole visual signature of a dune -- a
        # symmetric profile reads as corrugated cardboard.
        wind = np.clip(frac / (1.0 - slip), 0.0, 1.0)
        face = np.clip((frac - (1.0 - slip)) / slip, 0.0, 1.0)
        windward = 0.5 - 0.5 * np.cos(np.pi * wind)
        slipface = 1.0 - (0.5 - 0.5 * np.cos(np.pi * face))
        profile = np.where(frac < (1.0 - slip), windward, slipface)

        # Dune height varies along the field; without this every crest reaches
        # exactly the same altitude and the panel looks machined.
        envelope = 0.55 + 0.45 * (
            0.5
            + 0.5
            * noise.fbm(rx / (spacing * 3.5), ry / (spacing * 2.0), octaves=3, seed=seed + 77)
        )
        h = profile * envelope
        if ripples > 0.0:
            # Fine wind ripples running across the dune, strongest on the
            # windward slope where sand is actually moving.
            fine = np.sin(2.0 * np.pi * (rx + drift * 0.4) / (spacing * 0.055))
            h = h + ripples * 0.05 * fine * np.clip(1.0 - profile, 0.0, 1.0)
        return h

    return fn


def _hills(p: dict, bounds: Bounds, seed: int) -> HeightFn:
    scale = max(p["scale_mm"], 1e-6)
    octaves = int(p["octaves"])
    gain = p["roughness"]
    warp_amount = p["warp"]

    def fn(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        wx, wy = noise.warp(
            x, y, amount=warp_amount * scale * 0.5, scale=scale * 2.0, seed=seed
        )
        return noise.fbm(wx / scale, wy / scale, octaves=octaves, gain=gain, seed=seed)

    return fn


def _mountains(p: dict, bounds: Bounds, seed: int) -> HeightFn:
    scale = max(p["scale_mm"], 1e-6)
    octaves = int(p["octaves"])
    sharpness = p["sharpness"]
    warp_amount = p["warp"]

    def fn(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        wx, wy = noise.warp(
            x, y, amount=warp_amount * scale * 0.35, scale=scale * 2.5, seed=seed + 13
        )
        return noise.ridged_fbm(
            wx / scale, wy / scale, octaves=octaves, sharpness=sharpness, seed=seed
        )

    return fn


def _canyon(p: dict, bounds: Bounds, seed: int) -> HeightFn:
    scale = max(p["scale_mm"], 1e-6)
    strata = max(int(p["strata"]), 1)
    erosion = p["erosion"]
    channel = p["channel"]

    def fn(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        wx, wy = noise.warp(
            x, y, amount=erosion * scale * 0.8, scale=scale * 1.6, seed=seed + 3
        )
        base = 0.5 + 0.5 * noise.fbm(wx / scale, wy / scale, octaves=5, seed=seed)
        # Sedimentary banding: quantise the surface, then let the erosion field
        # cut back into the steps so the bands are ragged rather than machined.
        stepped = np.floor(base * strata) / strata
        rough = noise.fbm(wx / (scale * 0.18), wy / (scale * 0.18), octaves=3, seed=seed + 41)
        h = stepped + rough * (0.6 / strata)
        if channel > 0.0:
            # A meandering river cut, deepest at its centre line. The meander is
            # a function of y alone, so the channel is a single connected course
            # down the panel rather than a field of unrelated gouges.
            meander = noise.fbm(
                y / (scale * 2.2), np.full_like(y, 0.5), octaves=3, seed=seed + 91
            )
            distance = np.abs(x - bounds.center[0] - meander * scale * 1.4)
            cut = np.exp(-((distance / (scale * 0.30)) ** 2))
            h = h - channel * cut * 0.85
        return h

    return fn


def _topographic(p: dict, bounds: Bounds, seed: int) -> HeightFn:
    scale = max(p["scale_mm"], 1e-6)
    contours = max(int(p["contours"]), 2)
    index_every = int(p["index_every"])
    line = p["line"]

    def fn(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        base = 0.5 + 0.5 * noise.fbm(x / scale, y / scale, octaves=5, seed=seed)
        scaled = base * contours
        band = np.floor(scaled)
        frac = scaled - band
        h = band / contours
        if line > 0.0:
            # A raised bead sitting on each contour boundary. Its width is
            # constant in *height* units, so it is narrow on a steep slope and
            # wide on a flat one -- which is exactly how a contour map behaves
            # and, usefully, keeps the bead printable on shallow ground.
            edge = np.minimum(frac, 1.0 - frac)
            bead = np.exp(-((edge / 0.07) ** 2))
            weight = np.ones_like(band)
            if index_every >= 2:
                # Index contours: every nth line heavier, as on a real map.
                weight = np.where(np.mod(band, index_every) < 0.5, 1.0, 0.55)
            h = h + line * (0.9 / contours) * bead * weight
        return h

    return fn


def _flow(p: dict, bounds: Bounds, seed: int) -> HeightFn:
    scale = max(p["scale_mm"], 1e-6)
    warp_amount = p["warp"]
    bands = int(p["bands"])
    octaves = int(p["octaves"])

    def fn(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        # Two warps in series. One warp stretches; a warp of a warp folds, and
        # folding is what produces the marbled sheets.
        wx, wy = noise.warp(x, y, amount=warp_amount * scale, scale=scale * 2.0, seed=seed)
        wx, wy = noise.warp(
            wx, wy, amount=warp_amount * scale * 0.45, scale=scale * 0.7, seed=seed + 617
        )
        base = noise.fbm(wx / scale, wy / scale, octaves=octaves, seed=seed + 5)
        if bands >= 2:
            return np.sin(base * np.pi * bands)
        return base

    return fn


# --------------------------------------------------------------------------
# Mathematical
# --------------------------------------------------------------------------


def _tpms(p: dict, bounds: Bounds, seed: int) -> HeightFn:
    cell = max(p["cell_mm"], 1e-6)
    surface = p["surface"]
    phase = p["phase"] * 2.0 * math.pi
    angle = p["angle_deg"]
    center = bounds.center

    def fn(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        rx, ry = rotate(x, y, angle, center)
        k = 2.0 * np.pi / cell
        a, b = rx * k, ry * k
        c = phase
        sa, ca = np.sin(a), np.cos(a)
        sb, cb = np.sin(b), np.cos(b)
        sc, cc = math.sin(c), math.cos(c)
        if surface == "gyroid":
            value = sa * cb + sb * cc + sc * ca
        elif surface == "schwarz_p":
            value = ca + cb + cc
        elif surface == "diamond":
            value = sa * sb * sc + sa * cb * cc + ca * sb * cc + ca * cb * sc
        else:  # neovius
            value = 3.0 * (ca + cb) + 4.0 * ca * cb * cc
        # The level set itself is the interesting object, so height is distance
        # from zero: the surface becomes the valley floor and the two labyrinths
        # either side of it become the raised ground.
        return np.abs(value)

    return fn


def _quasicrystal(p: dict, bounds: Bounds, seed: int) -> HeightFn:
    symmetry = max(int(p["symmetry"]), 2)
    wavelength = max(p["wavelength_mm"], 1e-6)
    phase = p["phase"] * 2.0 * math.pi
    angle = math.radians(p["angle_deg"])
    k = 2.0 * math.pi / wavelength
    directions = [
        (math.cos(angle + math.pi * i / symmetry), math.sin(angle + math.pi * i / symmetry))
        for i in range(symmetry)
    ]

    def fn(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        # N plane waves at equal angular spacing. For N = 5 (or any N whose
        # angles are incommensurate with a lattice) the sum never repeats: the
        # panel is genuinely aperiodic, which is the Penrose-tiling property
        # worth having on a wall.
        total = np.zeros_like(x, dtype=np.float64)
        for dx, dy in directions:
            total += np.cos(k * (x * dx + y * dy) + phase)
        return total

    return fn


def _chladni(p: dict, bounds: Bounds, seed: int) -> HeightFn:
    m = float(p["m"])
    n = float(p["n"])
    mix = p["mix"]
    # Chladni figures are a property of a plate of a particular size, so the
    # mode numbers are counted across the *panel*. A tile is a window onto that
    # plate, which is why the seams still line up.
    lx = max(bounds.width, 1e-6)
    ly = max(bounds.height, 1e-6)
    x0, y0 = bounds.x0, bounds.y0

    def fn(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        u = (x - x0) / lx
        v = (y - y0) / ly
        a = np.cos(n * np.pi * u) * np.cos(m * np.pi * v)
        b = np.cos(m * np.pi * u) * np.cos(n * np.pi * v)
        # The nodal lines -- where the sand collects -- are the zeros, so the
        # displacement magnitude is the relief.
        return np.abs(a - b + mix * (a + b))

    return fn


# Escape radius for the Julia iteration. Anything above 2 is outside the set;
# 4 leaves enough headroom for the smooth-escape correction to be meaningful.
_JULIA_ESCAPE = 4.0


def _julia(p: dict, bounds: Bounds, seed: int) -> HeightFn:
    c = complex(p["c_re"], p["c_im"])
    iterations = int(p["iterations"])
    zoom = max(p["zoom"], 1e-6)
    angle = p["angle_deg"]
    interior = p["interior"]
    center = bounds.center
    # Map the panel onto the complex plane so the classic [-1.6, 1.6] window
    # covers its shorter side at zoom 1.
    span = min(bounds.width, bounds.height) / 2.0
    scale = 1.6 / (span * zoom)

    def fn(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        rx, ry = rotate(x, y, angle, center)
        z = ((rx - center[0]) * scale) + 1j * ((ry - center[1]) * scale)
        escaped_at = np.full(z.shape, float(iterations))
        alive = np.ones(z.shape, dtype=bool)
        for i in range(iterations):
            z = np.where(alive, z * z + c, z)
            magnitude = np.abs(z)
            newly = alive & (magnitude > _JULIA_ESCAPE)
            if newly.any():
                # Smooth (fractional) escape time. The integer count gives
                # concentric terraces; the correction removes them, which
                # matters because those terraces are millimetres deep once this
                # is relief rather than a picture.
                #
                # Clamped at zero: a point whose first step lands at 1e30 gets a
                # correction larger than the iteration it escaped on, and the
                # negative escape time that follows becomes a NaN two lines
                # later. Zero is the honest answer -- it escaped immediately.
                smooth = np.log2(np.log(magnitude[newly]) / math.log(_JULIA_ESCAPE))
                escaped_at[newly] = np.maximum(i + 1.0 - smooth, 0.0)
                alive &= ~newly
            if not alive.any():
                break
        h = np.log1p(escaped_at) / math.log1p(iterations)
        if interior == "flat":
            h = np.where(alive, 1.0, h)
        elif interior == "sunken":
            h = np.where(alive, 0.0, h)
        return h

    return fn


def _spiral(p: dict, bounds: Bounds, seed: int) -> HeightFn:
    arms = max(int(p["arms"]), 1)
    pitch = max(p["pitch_mm"], 1e-6)
    kind = p["kind"]
    taper = p["taper"]
    center = bounds.center

    def fn(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        r, theta = polar(x, y, center)
        if kind == "logarithmic":
            # Equiangular: every turn is a constant *ratio* larger, so the
            # pattern is self-similar under scaling -- the nautilus rule.
            radial = np.log(np.maximum(r, 1e-6)) / (pitch / 40.0)
        else:
            radial = r / pitch
        h = np.cos(2.0 * np.pi * (radial - arms * theta / (2.0 * np.pi)))
        if taper > 0.0:
            # Fade the relief out towards the middle, where the arms converge
            # into a smudge no nozzle can resolve.
            h = h * (1.0 - taper * np.exp(-((r / (pitch * 1.5)) ** 2)))
        return h

    return fn


def _sierpinski(p: dict, bounds: Bounds, seed: int) -> HeightFn:
    cell = max(p["cell_mm"], 1e-6)
    depth = int(p["depth"])
    variant = p["variant"]
    angle = p["angle_deg"]
    center = bounds.center

    def fn(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        rx, ry = rotate(x, y, angle, center)
        if variant == "triangle":
            # Pascal's triangle mod 2: cell (i, j) is filled when i AND j has no
            # bits in common. The Sierpinski gasket falls out of the bitwise and.
            size = 1 << depth
            i = np.mod(np.floor(rx / cell).astype(np.int64), size)
            j = np.mod(np.floor(ry / cell).astype(np.int64), size)
            filled = (i & j) == 0
            return filled.astype(np.float64)
        # Carpet: base-3 digits, a hole wherever both digits are 1. Height is
        # graded by the level at which the hole appears, so the relief is a
        # staircase into the fractal instead of a binary mask.
        size = 3**depth
        i = np.mod(np.floor(rx / cell).astype(np.int64), size)
        j = np.mod(np.floor(ry / cell).astype(np.int64), size)
        level = np.full(i.shape, float(depth))
        for k in range(depth):
            di = np.mod(i // (3**k), 3)
            dj = np.mod(j // (3**k), 3)
            hole = (di == 1) & (dj == 1)
            level = np.where(hole, np.minimum(level, float(k)), level)
        return level / depth

    return fn


# --------------------------------------------------------------------------
# Tessellation
# --------------------------------------------------------------------------


def _voronoi(p: dict, bounds: Bounds, seed: int) -> HeightFn:
    cell = max(p["cell_mm"], 1e-6)
    jitter = p["jitter"]
    style = p["style"]

    def fn(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        f1, f2, value = noise.worley(x / cell, y / cell, seed=seed, jitter=jitter)
        if style == "domes":
            return np.clip(1.0 - f1 * 1.6, 0.0, 1.0)
        if style == "cracks":
            # The ridge midway between two seeds is the cell wall; f2 - f1 is
            # zero exactly there, so this draws the Voronoi diagram itself.
            return np.clip((f2 - f1) * 2.2, 0.0, 1.0)
        if style == "plates":
            # Flat-topped cells at cell-specific heights, with the boundary
            # kept as a groove so the plates read as separate stones.
            groove = np.clip((f2 - f1) * 7.0, 0.0, 1.0)
            return 0.25 + 0.75 * value * groove
        # pebbles: a rounded cap per cell, height varying cell to cell
        cap = np.clip(1.0 - f1 * 1.8, 0.0, 1.0) ** 0.6
        return cap * (0.55 + 0.45 * value)

    return fn


def _truchet(p: dict, bounds: Bounds, seed: int) -> HeightFn:
    cell = max(p["cell_mm"], 1e-6)
    width = max(p["line_mm"], 1e-6) / cell
    style = p["style"]
    angle = p["angle_deg"]
    center = bounds.center

    def fn(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        rx, ry = rotate(x, y, angle, center)
        u = rx / cell
        v = ry / cell
        i = np.floor(u).astype(np.int64)
        j = np.floor(v).astype(np.int64)
        fu = u - i
        fv = v - j
        flip = (noise.cell_hash(i, j, seed) & 1).astype(bool)

        # Two quarter-circle arcs of radius 1/2, either corner-to-corner one way
        # or the other. Because both variants meet every cell edge at its
        # midpoint, the arcs join continuously whichever way each cell fell --
        # that is the whole Truchet trick, and it is also what makes the pattern
        # survive being cut into tiles.
        d_a = np.abs(np.hypot(fu, fv) - 0.5)
        d_b = np.abs(np.hypot(1.0 - fu, 1.0 - fv) - 0.5)
        d_c = np.abs(np.hypot(1.0 - fu, fv) - 0.5)
        d_d = np.abs(np.hypot(fu, 1.0 - fv) - 0.5)
        distance = np.where(flip, np.minimum(d_a, d_b), np.minimum(d_c, d_d))

        t = np.clip(distance / width, 0.0, 1.0)
        ribbon = 0.5 + 0.5 * np.cos(np.pi * t)
        return 1.0 - ribbon if style == "groove" else ribbon

    return fn


def _hex_bumps(p: dict, bounds: Bounds, seed: int) -> HeightFn:
    cell = max(p["cell_mm"], 1e-6)
    style = p["style"]
    gap = float(np.clip(p["gap"], 0.0, 0.6))
    angle = p["angle_deg"]
    center = bounds.center

    def fn(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        rx, ry = rotate(x, y, angle, center)
        # Axial hex coordinates, rounded through cube coordinates -- the
        # standard way to find which hexagon a point is in without a search.
        size = cell / math.sqrt(3.0)
        q = (math.sqrt(3.0) / 3.0 * rx - ry / 3.0) / size
        r = (2.0 / 3.0 * ry) / size
        cq, cr = _hex_round(q, r)
        cx = size * (math.sqrt(3.0) * cq + math.sqrt(3.0) / 2.0 * cr)
        cy = size * (1.5 * cr)
        d = np.hypot(rx - cx, ry - cy) / (cell / 2.0)
        d = np.clip(d / max(1.0 - gap, 1e-6), 0.0, 1.0)
        if style == "cone":
            return 1.0 - d
        if style == "cup":
            return d**2
        if style == "flat_top":
            return np.clip(1.5 - 2.2 * d, 0.0, 1.0)
        return np.sqrt(np.clip(1.0 - d * d, 0.0, 1.0))  # dome

    return fn


def _hex_round(q: np.ndarray, r: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Round fractional axial hex coordinates to the containing hexagon."""
    x = q
    z = r
    y = -x - z
    rx = np.round(x)
    ry = np.round(y)
    rz = np.round(z)
    dx = np.abs(rx - x)
    dy = np.abs(ry - y)
    dz = np.abs(rz - z)
    # Re-derive whichever coordinate moved furthest, so the three always sum to
    # zero and the result is a real hex cell rather than a near miss.
    fix_x = (dx > dy) & (dx > dz)
    fix_z = ~fix_x & (dy > dz)
    rx = np.where(fix_x, -ry - rz, rx)
    rz = np.where(fix_z, -rx - ry, rz)
    return rx, rz


def _weave(p: dict, bounds: Bounds, seed: int) -> HeightFn:
    strip = max(p["strip_mm"], 1e-6)
    gap = max(p["gap_mm"], 0.0)
    crown = p["crown"]
    angle = p["angle_deg"]
    center = bounds.center
    period = strip + gap

    def fn(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        rx, ry = rotate(x, y, angle, center)
        u = rx / period
        v = ry / period
        i = np.floor(u).astype(np.int64)
        j = np.floor(v).astype(np.int64)
        fu = (u - i) * period
        fv = (v - j) * period

        # A rounded cross-section for each strip, zero where the gap is. At
        # crown 0 the strip is a flat slab; at 1 it is a full half-round.
        def band(t: np.ndarray) -> np.ndarray:
            u = np.clip(t / strip, 0.0, 1.0)
            profile = 1.0 - crown * (2.0 * u - 1.0) ** 2
            return np.where(t <= strip, profile, 0.0)

        horizontal = band(fv)
        vertical = band(fu)
        # Over-under alternation: the parity of the cell decides which strip is
        # on top, which is what turns two sets of ribs into a weave.
        over_h = np.mod(i + j, 2) == 0
        top = np.where(over_h, horizontal, vertical)
        bottom = np.where(over_h, vertical, horizontal) * 0.45
        return np.maximum(top, bottom)

    return fn


# --------------------------------------------------------------------------
# The catalogue
# --------------------------------------------------------------------------

_SPECS: list[PatternSpec] = [
    PatternSpec(
        id="waves",
        family="water",
        display_name="Wave Train",
        description=(
            "Parallel wave crests with steepened fronts. The straightforward one, "
            "and the one that reads best at a distance."
        ),
        kernel=_waves,
        tags=("waves", "water", "sine", "ocean", "ripple"),
        suggested_relief_mm=6.0,
        print_note=(
            "Crest above about 0.8 makes the front of each wave near-vertical; on a "
            "wall panel that is fine, on a flat-lying part it will need supports."
        ),
        params=(
            Param(
                "wavelength_mm",
                "number",
                45.0,
                "Crest-to-crest distance.",
                8.0,
                400.0,
                unit=" mm",
            ),
            _angle(0.0, "Direction the crests run."),
            Param(
                "crest",
                "number",
                0.35,
                "Steepens the wave front, flattens the trough.",
                0.0,
                1.0,
            ),
            Param(
                "harmonic",
                "number",
                0.18,
                "Second harmonic: adds a shoulder to each crest.",
                0.0,
                0.6,
            ),
            Param(
                "meander_mm",
                "number",
                6.0,
                "How far crest lines wander along their length.",
                0.0,
                60.0,
                unit=" mm",
            ),
        ),
    ),
    PatternSpec(
        id="ocean_swell",
        family="water",
        display_name="Ocean Swell",
        description=(
            "Several wave trains at spread headings, long ones dominant and short "
            "ones steep -- an actual sea state rather than one sine wave."
        ),
        kernel=_ocean,
        tags=("ocean", "sea", "swell", "waves", "water"),
        suggested_relief_mm=7.0,
        print_note=(
            "Chop adds fine detail; below about 1.5 mm of relief it disappears into the layer "
            "lines."
        ),
        params=(
            Param(
                "swell_mm",
                "number",
                120.0,
                "Wavelength of the dominant swell.",
                20.0,
                500.0,
                unit=" mm",
            ),
            Param("trains", "integer", 5, "How many wave trains are summed.", 2, 9),
            Param(
                "spread_deg",
                "number",
                35.0,
                "Angular spread between trains.",
                0.0,
                90.0,
                unit=" deg",
            ),
            Param(
                "chop", "number", 0.45, "Short-wave steepness and surface texture.", 0.0, 1.0
            ),
            _angle(0.0, "Heading of the dominant swell."),
        ),
    ),
    PatternSpec(
        id="ripples",
        family="water",
        display_name="Pond Ripples",
        description="Concentric rings spreading from one or more points, fading with distance.",
        kernel=_ripples,
        tags=("ripple", "pond", "water", "concentric", "rings"),
        suggested_relief_mm=4.0,
        print_note=(
            "Rings get tight near a source; keep the wavelength above ~8 mm or the centre "
            "fills in."
        ),
        params=(
            Param("wavelength_mm", "number", 18.0, "Ring spacing.", 6.0, 120.0, unit=" mm"),
            Param(
                "decay_mm",
                "number",
                220.0,
                "Distance over which a ring dies away.",
                20.0,
                2000.0,
                unit=" mm",
            ),
            Param("sources", "integer", 3, "Number of drop points.", 1, 6),
            Param(
                "spread_mm",
                "number",
                120.0,
                "How far from centre the extra drops land.",
                0.0,
                1000.0,
                unit=" mm",
            ),
        ),
    ),
    PatternSpec(
        id="interference",
        family="water",
        display_name="Interference",
        description=(
            "Point sources on a circle, summed. Produces moire fringes and hyperbolic "
            "nodal lines -- a physics demonstration you can hang up."
        ),
        kernel=_interference,
        tags=("interference", "moire", "fringes", "physics", "diffraction"),
        suggested_relief_mm=5.0,
        params=(
            Param("sources", "integer", 3, "Number of point sources.", 2, 8),
            Param(
                "wavelength_mm",
                "number",
                14.0,
                "Wavelength of each source.",
                4.0,
                80.0,
                unit=" mm",
            ),
            Param(
                "radius_mm",
                "number",
                90.0,
                "Radius of the circle the sources sit on.",
                5.0,
                800.0,
                unit=" mm",
            ),
            _angle(0.0, "Rotation of the source ring."),
        ),
    ),
    PatternSpec(
        id="sand_ripples",
        family="water",
        display_name="Sand Ripples",
        description="Fine wind ripples on a beach: crests that break, restart and drift.",
        kernel=_sand_ripples,
        tags=("sand", "beach", "ripples", "texture", "wind"),
        suggested_relief_mm=2.5,
        print_note=(
            "This is a texture, not a relief. Above ~4 mm deep it stops looking like sand."
        ),
        params=(
            Param("ripple_mm", "number", 12.0, "Ripple spacing.", 4.0, 60.0, unit=" mm"),
            Param("cross", "number", 0.3, "Strength of the secondary crossing set.", 0.0, 1.0),
            Param("wander", "number", 0.5, "How much the ripple field drifts.", 0.0, 1.0),
            _angle(0.0, "Wind direction."),
        ),
    ),
    PatternSpec(
        id="dunes",
        family="landscape",
        display_name="Sand Dunes",
        description=(
            "Asymmetric dune ridges: long windward slope, short steep slip face, "
            "meandering crest lines and varying dune height."
        ),
        kernel=_dunes,
        tags=("dunes", "desert", "sand", "landscape", "ridges"),
        suggested_relief_mm=8.0,
        print_note=(
            "Even at slip 0.45 the slip face is a shallow slope in absolute terms "
            "(dune spacing is tens of millimetres, relief is single digits), so this "
            "prints unsupported at any setting."
        ),
        params=(
            Param(
                "dune_mm", "number", 180.0, "Crest-to-crest spacing.", 40.0, 600.0, unit=" mm"
            ),
            Param(
                "slip",
                "number",
                0.28,
                "Fraction of the period taken by the steep slip face.",
                0.05,
                0.45,
            ),
            Param("meander", "number", 0.5, "How much crest lines wander.", 0.0, 1.5),
            Param(
                "ripples", "number", 0.25, "Fine wind ripples on the windward slope.", 0.0, 1.0
            ),
            _angle(0.0, "Wind direction."),
        ),
    ),
    PatternSpec(
        id="hills",
        family="landscape",
        display_name="Rolling Hills",
        description=(
            "Smooth fractal terrain. The gentlest thing in the catalogue and the easiest to "
            "print."
        ),
        kernel=_hills,
        tags=("hills", "terrain", "landscape", "noise", "smooth"),
        suggested_relief_mm=7.0,
        params=(
            _scale(150.0, 20.0, 1000.0, "Size of the largest landforms."),
            _octaves(4),
            Param(
                "roughness",
                "number",
                0.5,
                "How much each finer octave contributes.",
                0.25,
                0.75,
            ),
            Param(
                "warp", "number", 0.3, "Domain warp: makes the hills lean and fold.", 0.0, 1.5
            ),
        ),
    ),
    PatternSpec(
        id="mountains",
        family="landscape",
        display_name="Mountain Ridges",
        description="Ridged multifractal terrain: sharp crests, branching valleys.",
        kernel=_mountains,
        tags=("mountains", "ridges", "terrain", "landscape", "alpine"),
        suggested_relief_mm=9.0,
        print_note=(
            "Sharpness above ~2.5 makes knife-edge crests; expect them to come out as a single "
            "bead of plastic."
        ),
        params=(
            _scale(180.0, 20.0, 1000.0, "Size of the largest ridge systems."),
            _octaves(5),
            Param("sharpness", "number", 2.0, "Crest sharpness.", 1.0, 3.5),
            Param("warp", "number", 0.35, "Domain warp: bends ridge lines.", 0.0, 1.5),
        ),
    ),
    PatternSpec(
        id="canyon",
        family="landscape",
        display_name="Canyon Strata",
        description="Eroded sedimentary banding with an optional river cut through it.",
        kernel=_canyon,
        tags=("canyon", "strata", "erosion", "layers", "landscape"),
        suggested_relief_mm=8.0,
        params=(
            _scale(200.0, 30.0, 1000.0, "Size of the landforms."),
            Param("strata", "integer", 9, "Number of sedimentary bands.", 2, 40),
            Param("erosion", "number", 0.55, "How ragged the band edges are.", 0.0, 1.5),
            Param("channel", "number", 0.5, "Depth of the river cut. 0 for none.", 0.0, 1.0),
        ),
    ),
    PatternSpec(
        id="topographic",
        family="landscape",
        display_name="Topographic Map",
        description=(
            "Terrain flattened into contour terraces with a raised bead on every contour line."
        ),
        kernel=_topographic,
        tags=("topographic", "contour", "map", "terraces", "landscape"),
        suggested_relief_mm=6.0,
        print_note=(
            "Terraces are the most forgiving thing here to print: every surface is "
            "either flat or a riser, so there are no shallow slopes to show layer lines."
        ),
        params=(
            _scale(220.0, 40.0, 1000.0, "Size of the landforms."),
            Param("contours", "integer", 12, "Number of contour levels.", 3, 40),
            Param(
                "index_every",
                "integer",
                5,
                "Heavier index contour every n lines. 0 for uniform.",
                0,
                10,
            ),
            Param("line", "number", 0.8, "Height of the contour bead.", 0.0, 1.0),
        ),
    ),
    PatternSpec(
        id="flow",
        family="landscape",
        display_name="Flow / Marble",
        description="Doubly warped noise: folded sheets, like marble or a slow-moving fluid.",
        kernel=_flow,
        tags=("flow", "marble", "fluid", "swirl", "organic"),
        suggested_relief_mm=5.0,
        params=(
            _scale(140.0, 20.0, 800.0, "Size of the largest folds."),
            Param("warp", "number", 1.1, "Folding strength.", 0.0, 3.0),
            Param(
                "bands", "integer", 6, "Banding across the flow. 0 for a smooth surface.", 0, 40
            ),
            _octaves(4),
        ),
    ),
    PatternSpec(
        id="tpms",
        family="mathematical",
        display_name="Minimal Surface (TPMS)",
        description=(
            "A slice through a triply periodic minimal surface -- gyroid, Schwarz P, "
            "diamond or Neovius. The labyrinth pattern 3D printing is named for."
        ),
        kernel=_tpms,
        tags=("gyroid", "tpms", "minimal surface", "schwarz", "mathematical", "lattice"),
        suggested_relief_mm=5.0,
        print_note=(
            "Cell size under ~15 mm puts the thinnest part of the labyrinth near the nozzle "
            "width."
        ),
        params=(
            Param("cell_mm", "number", 40.0, "Unit cell size.", 8.0, 200.0, unit=" mm"),
            Param(
                "surface",
                "choice",
                "gyroid",
                "Which minimal surface.",
                choices=("gyroid", "schwarz_p", "diamond", "neovius"),
            ),
            Param(
                "phase",
                "number",
                0.0,
                "Which slice through the cell, as a fraction of it.",
                0.0,
                1.0,
            ),
            _angle(0.0, "Rotation."),
        ),
    ),
    PatternSpec(
        id="quasicrystal",
        family="mathematical",
        display_name="Quasicrystal",
        description=(
            "N plane waves at equal angular spacing. At five-fold symmetry the pattern "
            "never repeats -- a Penrose-like field with no unit cell at all."
        ),
        kernel=_quasicrystal,
        tags=("quasicrystal", "penrose", "aperiodic", "symmetry", "mathematical"),
        suggested_relief_mm=4.0,
        print_note=(
            "Odd symmetries (5, 7, 11) are the aperiodic ones; even ones give ordinary "
            "lattices."
        ),
        params=(
            Param(
                "symmetry",
                "integer",
                5,
                "Number of plane waves. Odd values are aperiodic.",
                2,
                12,
            ),
            Param(
                "wavelength_mm", "number", 18.0, "Plane wave period.", 4.0, 100.0, unit=" mm"
            ),
            Param("phase", "number", 0.0, "Phase offset, as a fraction of a period.", 0.0, 1.0),
            _angle(0.0, "Rotation."),
        ),
    ),
    PatternSpec(
        id="chladni",
        family="mathematical",
        display_name="Chladni Figure",
        description=(
            "The standing-wave modes of a vibrating plate. The valleys are the nodal "
            "lines where the sand collects in the classic demonstration."
        ),
        kernel=_chladni,
        tags=("chladni", "standing wave", "modes", "physics", "mathematical"),
        suggested_relief_mm=5.0,
        print_note=(
            "Mode numbers are counted across the whole panel, so a 3x2 tiling still shows one "
            "coherent figure."
        ),
        params=(
            Param("m", "integer", 3, "First mode number.", 1, 14),
            Param("n", "integer", 5, "Second mode number.", 1, 14),
            Param(
                "mix",
                "number",
                0.0,
                "Blends the two mode orderings; 0 is the classic figure.",
                -1.0,
                1.0,
            ),
        ),
    ),
    PatternSpec(
        id="julia",
        family="mathematical",
        display_name="Julia Set",
        description=(
            "Smooth escape-time relief of z^2 + c. Self-similar detail at every scale the "
            "nozzle can resolve."
        ),
        kernel=_julia,
        tags=("julia", "fractal", "mandelbrot", "complex", "mathematical"),
        suggested_relief_mm=5.0,
        print_note=(
            "Detail is unbounded but the nozzle is not: past about 200 iterations at "
            "zoom 1 the extra structure is finer than 0.4 mm and prints as a flat smear."
        ),
        params=(
            Param("c_re", "number", -0.4, "Real part of c.", -2.0, 2.0),
            Param("c_im", "number", 0.6, "Imaginary part of c.", -2.0, 2.0),
            Param("zoom", "number", 1.0, "Zoom into the set.", 0.2, 8.0),
            Param("iterations", "integer", 120, "Escape-time iteration limit.", 16, 400),
            Param(
                "interior",
                "choice",
                "flat",
                "What the set's interior does: stand proud, sink, or blend.",
                choices=("flat", "sunken", "blend"),
            ),
            _angle(0.0, "Rotation."),
        ),
    ),
    PatternSpec(
        id="spiral",
        family="mathematical",
        display_name="Spiral Arms",
        description="Archimedean or logarithmic spiral ridges. One arm or twelve.",
        kernel=_spiral,
        tags=("spiral", "logarithmic", "nautilus", "arms", "mathematical"),
        suggested_relief_mm=5.0,
        params=(
            Param("arms", "integer", 5, "Number of spiral arms.", 1, 16),
            Param(
                "pitch_mm",
                "number",
                26.0,
                "Radial distance between turns.",
                5.0,
                200.0,
                unit=" mm",
            ),
            Param(
                "kind",
                "choice",
                "archimedean",
                "Constant spacing, or constant ratio (self-similar).",
                choices=("archimedean", "logarithmic"),
            ),
            Param("taper", "number", 0.7, "Fades the unresolvable centre to flat.", 0.0, 1.0),
        ),
    ),
    PatternSpec(
        id="sierpinski",
        family="mathematical",
        display_name="Sierpinski",
        description=(
            "The gasket from Pascal's triangle mod 2, or the carpet graded by hole depth."
        ),
        kernel=_sierpinski,
        tags=("sierpinski", "fractal", "gasket", "carpet", "mathematical"),
        suggested_relief_mm=4.0,
        print_note=(
            "Every face is flat and every wall vertical: this is the easiest pattern here to "
            "print cleanly."
        ),
        params=(
            Param(
                "cell_mm", "number", 6.0, "Size of the smallest cell.", 1.5, 40.0, unit=" mm"
            ),
            Param("depth", "integer", 4, "Recursion depth before the pattern repeats.", 1, 6),
            Param(
                "variant",
                "choice",
                "carpet",
                "Gasket (binary) or carpet (graded by depth).",
                choices=("carpet", "triangle"),
            ),
            _angle(0.0, "Rotation."),
        ),
    ),
    PatternSpec(
        id="voronoi",
        family="tessellation",
        display_name="Voronoi Cells",
        description="Cellular noise as domes, cracks, flat plates or pebbles.",
        kernel=_voronoi,
        tags=("voronoi", "cells", "worley", "organic", "pebbles", "cracks"),
        suggested_relief_mm=5.0,
        params=(
            Param("cell_mm", "number", 26.0, "Average cell size.", 5.0, 120.0, unit=" mm"),
            Param(
                "jitter",
                "number",
                0.9,
                "How irregular the cells are. 0 gives a square grid.",
                0.0,
                1.0,
            ),
            Param(
                "style",
                "choice",
                "pebbles",
                "How the cells are rendered.",
                choices=("pebbles", "domes", "cracks", "plates"),
            ),
        ),
    ),
    PatternSpec(
        id="truchet",
        family="tessellation",
        display_name="Truchet Tiles",
        description=(
            "Quarter-circle arcs, randomly flipped per cell. Every arc meets its "
            "neighbour at an edge midpoint, so the ribbons never break."
        ),
        kernel=_truchet,
        tags=("truchet", "maze", "arcs", "labyrinth", "tessellation"),
        suggested_relief_mm=4.0,
        params=(
            Param("cell_mm", "number", 25.0, "Tile size.", 6.0, 120.0, unit=" mm"),
            Param("line_mm", "number", 7.0, "Ribbon width.", 1.0, 40.0, unit=" mm"),
            Param(
                "style",
                "choice",
                "ribbon",
                "Raised ribbon or cut groove.",
                choices=("ribbon", "groove"),
            ),
            _angle(0.0, "Rotation."),
        ),
    ),
    PatternSpec(
        id="hex_bumps",
        family="tessellation",
        display_name="Hex Field",
        description="A honeycomb of domes, cones, cups or flat-topped mesas.",
        kernel=_hex_bumps,
        tags=("hexagon", "honeycomb", "bumps", "domes", "tessellation"),
        suggested_relief_mm=5.0,
        params=(
            Param(
                "cell_mm", "number", 22.0, "Hexagon across-flats size.", 4.0, 120.0, unit=" mm"
            ),
            Param(
                "style",
                "choice",
                "dome",
                "Cross-section of each cell.",
                choices=("dome", "cone", "cup", "flat_top"),
            ),
            Param(
                "gap",
                "number",
                0.12,
                "Flat land between cells, as a fraction of the cell.",
                0.0,
                0.6,
            ),
            _angle(0.0, "Rotation."),
        ),
    ),
    PatternSpec(
        id="weave",
        family="tessellation",
        display_name="Basket Weave",
        description="Two sets of rounded strips, over and under, alternating by cell.",
        kernel=_weave,
        tags=("weave", "basket", "textile", "woven", "tessellation"),
        suggested_relief_mm=4.0,
        print_note=(
            "The under-strip sits at 45% of the relief, so the over-under reads even at 3 mm."
        ),
        params=(
            Param("strip_mm", "number", 18.0, "Width of each strip.", 4.0, 80.0, unit=" mm"),
            Param("gap_mm", "number", 2.0, "Gap between strips.", 0.0, 20.0, unit=" mm"),
            Param("crown", "number", 0.8, "How rounded each strip is.", 0.0, 1.0),
            _angle(0.0, "Rotation."),
        ),
    ),
]

PATTERNS: dict[str, PatternSpec] = {spec.id: spec for spec in _SPECS}


def get_pattern(pattern_id: str) -> PatternSpec:
    """Look up a pattern, with a listing in the error rather than a KeyError."""
    try:
        return PATTERNS[pattern_id]
    except KeyError:
        raise KeyError(
            f"unknown pattern {pattern_id!r}; known patterns: {', '.join(sorted(PATTERNS))}"
        ) from None


def build_field_fn(
    pattern_id: str,
    params: dict[str, Any] | None,
    bounds: Bounds,
    seed: int,
) -> tuple[HeightFn, dict[str, Any], PatternSpec]:
    """Resolve parameters and instantiate the kernel for one panel."""
    spec = get_pattern(pattern_id)
    resolved = resolve_params(spec, params)
    return spec.kernel(resolved, bounds, seed), resolved, spec
