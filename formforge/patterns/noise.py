"""Deterministic noise, evaluated in world coordinates.

Every function here takes world-space millimetre coordinates and returns a
value. That is the whole trick behind seamless tiling: a tile is not generated
and then matched to its neighbour, it is a *window onto one global field*, so
the crest of a dune that leaves tile B2 at x = 400 mm arrives at tile B3 at
x = 400 mm because both tiles asked the same function about the same point.
Nothing has to line anything up afterwards, and there is no seam-blending pass
that could fail.

Two consequences follow, and both are load-bearing:

* **No state, no RNG objects.** Values come from an integer hash of the lattice
  cell, so sampling a point is independent of what was sampled before it. A
  `numpy.random` generator would give a different field depending on the order
  tiles were built in, which is exactly the bug that makes a wall panel not fit
  together.
* **Seeds are part of the design, not of the run.** The same seed reproduces
  the same panel on another machine a year later, which matters when someone
  cracks a tile and needs to reprint that one tile.

Everything is vectorised over numpy arrays: a 200 mm tile at 0.4 mm sampling is
250 000 points, and a six-octave fBm over that is six passes of array maths.
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "cell_hash",
    "fbm",
    "gradient_noise",
    "hash_unit",
    "ridged_fbm",
    "value_noise",
    "warp",
    "worley",
]

# 32-bit mixing constants. Any odd multiplier with well-spread bits works; these
# are the usual suspects from the integer-hash literature.
_MASK = 0xFFFFFFFF
_PRIME_X = 374761393
_PRIME_Y = 668265263
_PRIME_S = 1442695041


def _hash2(ix: np.ndarray, iy: np.ndarray, seed: int) -> np.ndarray:
    """Hash a lattice cell to a well-distributed 32-bit integer.

    int64 arithmetic with an explicit mask rather than uint32 wraparound: the
    mask says what is happening, and it behaves the same on every numpy
    version rather than depending on overflow semantics.
    """
    h = (ix.astype(np.int64) * _PRIME_X) ^ (iy.astype(np.int64) * _PRIME_Y)
    h = (h + int(seed) * _PRIME_S) & _MASK
    h = ((h ^ (h >> 13)) * 1274126177) & _MASK
    return (h ^ (h >> 16)) & _MASK


def cell_hash(ix: np.ndarray, iy: np.ndarray, seed: int = 0) -> np.ndarray:
    """A stable 32-bit integer for an integer cell -- the public face of `_hash2`.

    Patterns built on a grid of cells (Truchet, brick bonds, anything that flips
    a coin per cell) need the same coin every time that cell is sampled, from
    whichever tile is asking.
    """
    return _hash2(np.asarray(ix), np.asarray(iy), seed)


def hash_unit(index: int, seed: int = 0) -> float:
    """One reproducible float in [0, 1) from an integer index.

    For the handful of values a pattern needs *once per panel* rather than once
    per sample: a wave train's phase, where a raindrop fell. A numpy Generator
    would be just as reproducible, but this keeps the module's promise literal
    -- every number in a pattern comes from the same hash -- so there is no
    second source of randomness to keep in step with this one.
    """
    return float(_unit(_hash2(np.asarray(int(index)), np.asarray(0), seed)))


def _unit(h: np.ndarray) -> np.ndarray:
    """A hash as a float in [0, 1)."""
    return h.astype(np.float64) / 4294967296.0


def _fade(t: np.ndarray) -> np.ndarray:
    """Quintic smoothstep, 6t^5 - 15t^4 + 10t^3.

    Its first *and* second derivatives vanish at the lattice points. The cubic
    smoothstep only kills the first, which leaves a visible crease along every
    integer line -- invisible on screen, and a ridge you can feel with a
    fingernail once it is 4 mm of relief in PLA.
    """
    return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)


def value_noise(x: np.ndarray, y: np.ndarray, *, seed: int = 0) -> np.ndarray:
    """Lattice value noise in [0, 1]. Soft and rounded; good for dunes."""
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    fx = _fade(x - x0)
    fy = _fade(y - y0)

    v00 = _unit(_hash2(x0, y0, seed))
    v10 = _unit(_hash2(x0 + 1, y0, seed))
    v01 = _unit(_hash2(x0, y0 + 1, seed))
    v11 = _unit(_hash2(x0 + 1, y0 + 1, seed))

    top = v00 + (v10 - v00) * fx
    bottom = v01 + (v11 - v01) * fx
    return top + (bottom - top) * fy


# Sixteen evenly spaced gradient directions. A small fixed set beats uniformly
# random vectors: the directional bias averages out over octaves, and the dot
# products stay in a predictable range so the normalisation below is honest.
_ANGLES = np.arange(16) * (2.0 * np.pi / 16.0)
_GRAD = np.stack([np.cos(_ANGLES), np.sin(_ANGLES)], axis=-1)


def gradient_noise(x: np.ndarray, y: np.ndarray, *, seed: int = 0) -> np.ndarray:
    """Perlin-style gradient noise in [-1, 1]. Sharper ridges than value noise."""
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    dx = x - x0
    dy = y - y0
    fx = _fade(dx)
    fy = _fade(dy)

    def dot(ix: np.ndarray, iy: np.ndarray, ox: int, oy: int) -> np.ndarray:
        g = _GRAD[_hash2(ix, iy, seed) & 15]
        return g[..., 0] * (dx - ox) + g[..., 1] * (dy - oy)

    n00 = dot(x0, y0, 0, 0)
    n10 = dot(x0 + 1, y0, 1, 0)
    n01 = dot(x0, y0 + 1, 0, 1)
    n11 = dot(x0 + 1, y0 + 1, 1, 1)

    top = n00 + (n10 - n00) * fx
    bottom = n01 + (n11 - n01) * fx
    # The theoretical bound for 2D gradient noise is sqrt(2)/2; scaling by its
    # reciprocal puts the output on [-1, 1] instead of [-0.707, 0.707].
    return (top + (bottom - top) * fy) * 1.4142135623730951


def fbm(
    x: np.ndarray,
    y: np.ndarray,
    *,
    octaves: int = 5,
    lacunarity: float = 2.0,
    gain: float = 0.5,
    seed: int = 0,
) -> np.ndarray:
    """Fractional Brownian motion: octaves of gradient noise, in [-1, 1].

    Each octave doubles the frequency and halves the amplitude, which is the
    self-similarity that makes the result read as terrain rather than as a
    lumpy sine. The sum is divided by the total amplitude so the range does not
    depend on the octave count -- otherwise turning detail up also turns the
    relief depth up, and the print no longer matches the preview.
    """
    total = np.zeros_like(x, dtype=np.float64)
    amplitude = 1.0
    frequency = 1.0
    norm = 0.0
    for octave in range(max(1, int(octaves))):
        total += amplitude * gradient_noise(
            x * frequency, y * frequency, seed=seed + octave * 101
        )
        norm += amplitude
        amplitude *= gain
        frequency *= lacunarity
    return total / norm


def ridged_fbm(
    x: np.ndarray,
    y: np.ndarray,
    *,
    octaves: int = 5,
    lacunarity: float = 2.0,
    gain: float = 0.5,
    sharpness: float = 2.0,
    seed: int = 0,
) -> np.ndarray:
    """Ridged multifractal in [0, 1]: `1 - |noise|`, weighted by the octave above.

    Folding the noise at zero turns every zero crossing into a crest, which is
    what makes mountain ridges rather than rolling hills. The weighting by the
    previous octave is what keeps detail in the valleys from competing with the
    ridges -- without it the result is busy everywhere and prints as mush.
    """
    total = np.zeros_like(x, dtype=np.float64)
    amplitude = 1.0
    frequency = 1.0
    norm = 0.0
    weight = np.ones_like(x, dtype=np.float64)
    for octave in range(max(1, int(octaves))):
        signal = 1.0 - np.abs(
            gradient_noise(x * frequency, y * frequency, seed=seed + octave * 97)
        )
        signal = signal**sharpness
        signal = signal * np.clip(weight, 0.0, 1.0)
        weight = signal * 2.0
        total += amplitude * signal
        norm += amplitude
        amplitude *= gain
        frequency *= lacunarity
    return total / norm


def worley(
    x: np.ndarray,
    y: np.ndarray,
    *,
    seed: int = 0,
    jitter: float = 1.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Cellular (Worley) noise.

    Returns the distance to the nearest seed, the distance to the
    second-nearest, and a per-cell random value belonging to the *winning*
    cell. The third one is what makes flat-topped cells possible: without a
    stable identity for the cell a point belongs to, "give every cell its own
    height" has nothing to hash.

    One jittered seed per unit cell, searched over the 3x3 neighbourhood. That
    neighbourhood is sufficient only while `jitter <= 1`; beyond that a seed two
    cells away could win and the field would develop discontinuities that show
    up as a step across a tile seam, so the jitter is clamped rather than
    trusted.
    """
    jitter = float(np.clip(jitter, 0.0, 1.0))
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)

    first = np.full(x.shape, np.inf)
    second = np.full(x.shape, np.inf)
    value = np.zeros(x.shape, dtype=np.float64)
    for oy in (-1, 0, 1):
        for ox in (-1, 0, 1):
            cx = x0 + ox
            cy = y0 + oy
            hx = _unit(_hash2(cx, cy, seed))
            hy = _unit(_hash2(cx, cy, seed + 7919))
            px = cx + 0.5 + (hx - 0.5) * jitter
            py = cy + 0.5 + (hy - 0.5) * jitter
            d = np.hypot(px - x, py - y)
            closer = d < first
            second = np.minimum(second, np.maximum(first, d))
            first = np.where(closer, d, first)
            value = np.where(closer, _unit(_hash2(cx, cy, seed + 104729)), value)
    return first, second, value


def warp(
    x: np.ndarray,
    y: np.ndarray,
    *,
    amount: float,
    scale: float,
    seed: int = 0,
    octaves: int = 3,
) -> tuple[np.ndarray, np.ndarray]:
    """Domain warp: displace the sampling coordinates by a second noise field.

    This is the cheapest way to get something that looks eroded rather than
    generated. Straight fBm has an obvious statistical uniformity; pushing its
    input around with more fBm produces the stretched, folded, marbled
    structure that real flow leaves behind.
    """
    if amount <= 0.0 or scale <= 0.0:
        return x, y
    fx = fbm(x / scale, y / scale, octaves=octaves, seed=seed + 1301)
    fy = fbm(x / scale, y / scale, octaves=octaves, seed=seed + 5417)
    return x + fx * amount, y + fy * amount
