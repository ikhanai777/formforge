"""The height field: one continuous surface, sampled per tile.

A pattern is a function from a point on the wall to a height in [0, 1]. Nothing
in it knows about tiles. The tiling layer decides *where* to sample; this layer
decides *what* the answer is. Keeping those apart is what makes a nine-tile
panel and a one-piece panel the same geometry.

The one thing that cannot be done per tile is **normalisation**. A dune field
is `fbm(...)`, whose actual range over a particular 600 x 400 mm wall is not
knowable in closed form, so the raw values have to be mapped onto [0, 1] by
measuring them. Measure that per tile and every tile gets its own mapping: the
peaks of the flattest tile stretch to full relief, the seam steps by
millimetres, and the panel is scrap. So the range is measured **once over the
whole panel** and then shared, which is `calibrate()` below and the reason
`PatternField` is mutable state rather than a bare function.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dataclass_field

import numpy as np

__all__ = ["Bounds", "HeightFn", "PatternField"]

# A pattern kernel: world millimetres in, unnormalised height out.
HeightFn = Callable[[np.ndarray, np.ndarray], np.ndarray]


@dataclass(frozen=True)
class Bounds:
    """An axis-aligned window on the wall, in millimetres."""

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x0 + self.x1) / 2.0, (self.y0 + self.y1) / 2.0)

    @classmethod
    def sized(cls, width: float, height: float) -> Bounds:
        """A panel of this size, centred on the origin.

        Centred rather than corner-anchored because several patterns are
        radial: a ripple or a Julia set wants its interesting part in the
        middle of the wall, not at one corner.
        """
        return cls(-width / 2.0, -height / 2.0, width / 2.0, height / 2.0)

    def inset(self, amount: float) -> Bounds:
        return Bounds(self.x0 + amount, self.y0 + amount, self.x1 - amount, self.y1 - amount)


# Width of the soft shoulder at each end of the normalised range. Narrow on
# purpose: at 2% the shoulder is thinner than one layer of most of the relief
# depths anyone uses, so a pattern that is genuinely binary -- Sierpinski,
# Truchet -- still lands flat on its floor once the surface is snapped to layer
# multiples, while a fractal terrain keeps its peaks distinguishable.
_SHOULDER = 0.02


def _saturate(t: np.ndarray) -> np.ndarray:
    """Map the real line into [0, 1], identity across the middle.

    Inside [_SHOULDER, 1 - _SHOULDER] this is the identity, so the pattern is reproduced
    exactly where nearly all of it lives. Outside, it decays exponentially
    towards the bound it is approaching, matching value *and* slope at the
    join. Two properties matter and neither is decoration:

    * It is strictly increasing, so two peaks of different heights stay
      different heights. A hard clip makes them one flat mesa, and a flat mesa
      is the single most obvious "generated" artifact on a relief print.
    * It never exceeds [0, 1], so the relief depth the caller asked for is the
      relief depth they get, whatever the outlier at the far corner of the
      panel was doing.
    """
    m = _SHOULDER
    t = np.asarray(t, dtype=np.float64)
    high = 1.0 - m * np.exp(-(t - (1.0 - m)) / m)
    low = m * np.exp((t - m) / m)
    return np.where(t > 1.0 - m, high, np.where(t < m, low, t))


def _smoothstep(edge0: float, edge1: float, x: np.ndarray) -> np.ndarray:
    if edge1 <= edge0:
        return np.ones_like(x)
    t = np.clip((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


@dataclass
class PatternField:
    """A pattern kernel plus the finishing that turns it into printable relief.

    The finishing operations are deliberately few. Each one is here because it
    changes how the panel *prints*, not how it looks in a preview:

    * `contrast` redistributes material between crest and trough. Above 1 the
      troughs flatten and the crests stay, which is what you want when the
      relief is shallow enough that mid-tones turn into unreadable mush.
    * `terraces` quantises the surface into contour steps. A stepped surface has
      no shallow slopes left, and shallow slopes are where FDM layer lines are
      ugliest -- so terracing is both a look and a fix.
    * `border_mm` tapers the relief to flat around the outside of the *panel*,
      which gives the outer tiles a clean flat rim to sit against the wall and
      keeps the pattern from being cut off mid-crest at the panel edge.
    """

    fn: HeightFn
    bounds: Bounds
    invert: bool = False
    contrast: float = 1.0
    terraces: int = 0
    terrace_sharpness: float = 0.75
    border_mm: float = 0.0
    # Filled in by calibrate(); the raw range of `fn` over the whole panel.
    lo: float = dataclass_field(default=0.0)
    hi: float = dataclass_field(default=1.0)
    calibrated: bool = dataclass_field(default=False)

    # How many samples per axis the calibration pass uses. 192 x 192 over a
    # whole panel is ~37k points -- milliseconds -- and dense enough that the
    # measured range is within a percent or so of the true one for every
    # pattern in the library.
    CALIBRATION_SAMPLES: int = 192

    def calibrate(self) -> PatternField:
        """Measure the raw range of the kernel over the whole panel."""
        n = self.CALIBRATION_SAMPLES
        xs = np.linspace(self.bounds.x0, self.bounds.x1, n)
        ys = np.linspace(self.bounds.y0, self.bounds.y1, n)
        gx, gy = np.meshgrid(xs, ys, indexing="ij")
        raw = np.asarray(self.fn(gx, gy), dtype=np.float64)
        raw = raw[np.isfinite(raw)]
        if raw.size == 0:
            raise ValueError("the pattern produced no finite values over this panel")

        # Percentiles, not min and max. Fractal terrain spends most of its area
        # in the middle of its range and reaches the extremes on a handful of
        # peaks, so min/max normalisation leaves the bulk of a hillside using a
        # third of the available relief -- the panel comes out visibly flat
        # while the numbers say it used the full depth. Cutting at the half
        # percentile fixes that; `_saturate` below then folds the outliers back
        # in without flattening them.
        lo = float(np.percentile(raw, 0.5))
        hi = float(np.percentile(raw, 99.5))
        if hi - lo < 1e-9:
            lo, hi = float(raw.min()), float(raw.max())
        if hi - lo < 1e-9:
            # A constant field is legal -- `waves` with zero amplitude, say --
            # and must not divide by zero. Map it to flat rather than failing:
            # a flat panel is a defensible thing to ask for, and the caller
            # gets a plate of the requested size.
            hi = lo + 1.0
        self.lo, self.hi = lo, hi
        self.calibrated = True
        return self

    def sample(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        """Height in [0, 1] at world points (x, y)."""
        if not self.calibrated:
            raise RuntimeError(
                "PatternField.sample() before calibrate(): the normalisation range "
                "must be measured over the whole panel, or tiles will not match at "
                "their seams"
            )
        raw = np.asarray(self.fn(x, y), dtype=np.float64)
        # A pattern kernel that produces a NaN (an escape-time iteration that
        # overflows, a division by a zero radius at an exact centre point) would
        # otherwise become a NaN vertex, and a NaN vertex is a mesh no slicer
        # will open. Treat it as the floor.
        raw = np.nan_to_num(raw, nan=self.lo, posinf=self.hi, neginf=self.lo)
        h = _saturate((raw - self.lo) / (self.hi - self.lo))

        if self.invert:
            h = 1.0 - h
        if self.contrast != 1.0 and self.contrast > 0.0:
            h = h**self.contrast
        if self.terraces >= 2:
            h = self._terrace(h)
        if self.border_mm > 0.0:
            h = h * self._border_falloff(x, y)
        return np.clip(h, 0.0, 1.0)

    def _terrace(self, h: np.ndarray) -> np.ndarray:
        """Quantise into `terraces` contour steps with a controllable riser.

        `terrace_sharpness` at 1.0 gives vertical risers and dead-flat treads --
        a topographic map. Below that the riser keeps some slope, which prints
        with fewer visible seams on a 0.2 mm layer and is usually what people
        actually want from "contours".
        """
        levels = float(self.terraces)
        scaled = h * levels
        base = np.floor(scaled)
        frac = scaled - base
        k = float(np.clip(self.terrace_sharpness, 0.0, 0.999))
        # Push the fractional part towards a step: at k=0 this is the identity,
        # at k->1 it is a hard edge at the midpoint of each band.
        eased = _smoothstep(k * 0.5, 1.0 - k * 0.5, frac)
        return np.clip((base + eased) / levels, 0.0, 1.0)

    def _border_falloff(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        """Taper relief to zero within `border_mm` of the panel outline."""
        b = self.bounds
        d = np.minimum.reduce([x - b.x0, b.x1 - x, y - b.y0, b.y1 - y])
        return _smoothstep(0.0, self.border_mm, d)

    def sample_grid(self, bounds: Bounds, nx: int, ny: int) -> np.ndarray:
        """Sample a regular grid, returning an (nx, ny) array of heights."""
        xs = np.linspace(bounds.x0, bounds.x1, nx)
        ys = np.linspace(bounds.y0, bounds.y1, ny)
        gx, gy = np.meshgrid(xs, ys, indexing="ij")
        return self.sample(gx, gy)


def polar(
    x: np.ndarray, y: np.ndarray, center: tuple[float, float]
) -> tuple[np.ndarray, np.ndarray]:
    """Radius and angle about a centre -- the shared preamble of every radial pattern."""
    dx = x - center[0]
    dy = y - center[1]
    return np.hypot(dx, dy), np.arctan2(dy, dx)


def rotate(
    x: np.ndarray, y: np.ndarray, degrees: float, center: tuple[float, float] = (0.0, 0.0)
) -> tuple[np.ndarray, np.ndarray]:
    """Rotate sample coordinates about a centre.

    Rotating the *coordinates* rather than the pattern is what keeps a rotated
    wave train seamless across tiles: it is still one global function, just
    asked about a rotated point.
    """
    if degrees % 360.0 == 0.0:
        return x, y
    theta = math.radians(degrees)
    c, s = math.cos(theta), math.sin(theta)
    dx = x - center[0]
    dy = y - center[1]
    return center[0] + dx * c + dy * s, center[1] - dx * s + dy * c
