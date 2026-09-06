"""Turn an uploaded picture into a foreground mask.

This is the only part of FormForge that looks at pixels, and it is deliberately
the dumbest part of the feature. Everything downstream -- the outline, the rail,
the hooks, the mounts -- is exact parametric geometry; the image contributes one
thing and one thing only: *which pixels are the subject*. Getting a clean answer
to that question from a photograph is not solvable in general, so the strategy
here is to be right on the inputs people actually upload and to be legible when
it is wrong:

* **Alpha first.** A cut-out PNG already answers the question. If the file has a
  meaningful alpha channel, nothing else is consulted.
* **Otherwise, distance from the border colour.** The background of a product
  photo or a piece of clip art is whatever the edge of the frame is: a green
  screen, a white studio sweep, a beige wall. Measuring each pixel's colour
  distance from the median border colour and thresholding that (Otsu) handles
  all three, where a plain luminance threshold handles only the last one -- a
  pale wooden car on a green screen is *brighter* than its background, and a
  black van on a beige wall is darker.
* **Then connectivity.** Background is the part connected to the frame edge.
  Anything enclosed by the subject (the windows of a van, the counter of an "o")
  is a hole in the subject, not background, and is classified as such rather
  than by its colour.

Every stage is reported in `MaskInfo`, because "it traced the wrong thing" is
the failure mode that matters and the user needs to be told which assumption
produced it. `--invert` and `--threshold` exist for when it guesses wrong.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# Images are downsampled before tracing. 512 px on the long edge is far more
# resolution than a 200 mm plaque can express (a 0.4 mm nozzle lays down about
# 500 distinguishable strokes across that width), and it caps the cost of the
# whole-array passes below.
DEFAULT_MAX_DIM = 512

# Width of the border band sampled for the background colour, as a fraction of
# the shorter side. 4% is wide enough to average out JPEG noise and narrow
# enough that a subject touching the frame does not dominate it.
BORDER_BAND = 0.04

# An alpha channel is only believed when it actually separates something: a
# fully opaque channel is what every JPEG-converted-to-RGBA carries.
ALPHA_MIN_TRANSPARENT_FRACTION = 0.02


class ImageError(Exception):
    """The image could not be read, or carries nothing traceable."""


@dataclass
class MaskInfo:
    """How the mask was decided. Reported to the user, not just logged."""

    source: str = ""  # "alpha" | "colour-distance"
    threshold: float = 0.0
    inverted: bool = False
    foreground_fraction: float = 0.0
    original_size: tuple[int, int] = (0, 0)
    traced_size: tuple[int, int] = (0, 0)
    background_rgb: tuple[int, int, int] | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        payload = {
            "source": self.source,
            "threshold": round(self.threshold, 4),
            "inverted": self.inverted,
            "foreground_fraction": round(self.foreground_fraction, 4),
            "original_size_px": list(self.original_size),
            "traced_size_px": list(self.traced_size),
        }
        if self.background_rgb is not None:
            payload["background_rgb"] = list(self.background_rgb)
        if self.notes:
            payload["notes"] = list(self.notes)
        return payload


@dataclass
class Mask:
    """A boolean foreground mask, row 0 at the top of the picture."""

    array: np.ndarray
    info: MaskInfo

    @property
    def height(self) -> int:
        return int(self.array.shape[0])

    @property
    def width(self) -> int:
        return int(self.array.shape[1])

    @property
    def foreground_px(self) -> int:
        return int(self.array.sum())


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def _pillow():
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised by the error path
        raise ImageError(
            "reading an uploaded image needs Pillow, which is an optional "
            "dependency: pip install 'formforge[image]'"
        ) from exc
    return Image


def load_rgba(
    source: str | Path | bytes,
    *,
    max_dim: int = DEFAULT_MAX_DIM,
) -> tuple[np.ndarray, tuple[int, int]]:
    """Decode an image to an HxWx4 uint8 array, downsampled to `max_dim`.

    Returns the array and the original pixel size, which is worth reporting: a
    3000 px photo traced at 512 px is a deliberate choice, not a bug.
    """
    Image = _pillow()
    try:
        if isinstance(source, bytes):
            handle = Image.open(io.BytesIO(source))
        else:
            handle = Image.open(str(source))
        handle.load()
    except Exception as exc:
        raise ImageError(f"could not read the image: {exc}") from exc

    original = (int(handle.width), int(handle.height))
    if min(original) < 8:
        raise ImageError(
            f"the image is {original[0]}x{original[1]} px, too small to trace an "
            "outline from"
        )

    image = handle.convert("RGBA")
    longest = max(image.width, image.height)
    if max_dim and longest > max_dim:
        scale = max_dim / longest
        size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        # LANCZOS rather than NEAREST: the contour tracer reads a smoothed field,
        # and a box-filtered downsample is what makes its interpolation mean
        # anything. Nearest-neighbour would put the aliasing back.
        image = image.resize(size, Image.LANCZOS)

    return np.asarray(image, dtype=np.uint8), original


# ---------------------------------------------------------------------------
# Foreground extraction
# ---------------------------------------------------------------------------


def otsu_threshold(values: np.ndarray, bins: int = 256) -> float:
    """Otsu's between-class variance threshold over a 0..1 array.

    Implemented here rather than pulled from scikit-image: it is fifteen lines,
    and the alternative is a 30 MB dependency for one function.
    """
    flat = np.clip(values.reshape(-1).astype(np.float64), 0.0, 1.0)
    histogram, edges = np.histogram(flat, bins=bins, range=(0.0, 1.0))
    centres = (edges[:-1] + edges[1:]) / 2.0
    total = histogram.sum()
    if total == 0:
        return 0.5

    weight_bg = np.cumsum(histogram)
    weight_fg = total - weight_bg
    sum_all = np.cumsum(histogram * centres)
    grand_total = sum_all[-1]

    with np.errstate(invalid="ignore", divide="ignore"):
        mean_bg = np.where(weight_bg > 0, sum_all / np.maximum(weight_bg, 1), 0.0)
        mean_fg = np.where(
            weight_fg > 0, (grand_total - sum_all) / np.maximum(weight_fg, 1), 0.0
        )
        between = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2

    valid = (weight_bg > 0) & (weight_fg > 0)
    if not valid.any():
        return 0.5
    between = np.where(valid, between, -1.0)
    # The midpoint of the tied maximum rather than the first of it. On a real
    # photograph the maximum is unique and this changes nothing; on a clean
    # two-tone image every cut between the two values is equally good, and
    # taking the first one puts the threshold hard against the darker value,
    # where a single stray pixel flips into the foreground.
    best = np.flatnonzero(between >= between.max() - 1e-12)
    return float((centres[best[0]] + centres[best[-1]]) / 2.0)


def _border_colour(rgb: np.ndarray) -> tuple[np.ndarray, tuple[int, int, int]]:
    """The median colour of a band around the frame."""
    height, width = rgb.shape[:2]
    band = max(1, round(min(height, width) * BORDER_BAND))
    edges = np.concatenate(
        [
            rgb[:band].reshape(-1, 3),
            rgb[-band:].reshape(-1, 3),
            rgb[:, :band].reshape(-1, 3),
            rgb[:, -band:].reshape(-1, 3),
        ]
    )
    median = np.median(edges.astype(np.float64), axis=0)
    return median, tuple(round(float(c)) for c in median)  # type: ignore[return-value]


def foreground_mask(
    rgba: np.ndarray,
    *,
    threshold: float | None = None,
    invert: bool | None = None,
) -> tuple[np.ndarray, MaskInfo]:
    """Decide which pixels are the subject.

    `threshold` overrides the automatic one (0..1, against the normalised
    colour-distance field). `invert` forces the sense of the result, for the
    cases where the subject is what touches the frame.
    """
    info = MaskInfo()
    rgb = rgba[:, :, :3].astype(np.float64)
    alpha = rgba[:, :, 3]

    transparent_fraction = float((alpha < 128).mean())
    if transparent_fraction >= ALPHA_MIN_TRANSPARENT_FRACTION:
        field_ = alpha.astype(np.float64) / 255.0
        info.source = "alpha"
        info.notes.append(
            f"used the alpha channel ({transparent_fraction:.0%} of the image is "
            "transparent)"
        )
        cut = 0.5 if threshold is None else float(threshold)
    else:
        background, background_rgb = _border_colour(rgb)
        distance = np.sqrt(((rgb - background) ** 2).sum(axis=2))
        # Normalise by the observed maximum rather than the theoretical 441:
        # a subject that differs from the background by only 40 units still has
        # to be separable.
        peak = float(distance.max())
        field_ = distance / peak if peak > 1e-9 else np.zeros_like(distance)
        info.source = "colour-distance"
        info.background_rgb = background_rgb
        info.notes.append(
            "no usable alpha channel; separated the subject by colour distance "
            f"from the border colour rgb{background_rgb}"
        )
        cut = otsu_threshold(field_) if threshold is None else float(threshold)

    info.threshold = cut
    mask = field_ > cut

    if invert is None:
        # A mask covering most of the frame is almost always the background:
        # people photograph a small object on a large surface, and clip art sits
        # on a white field. The exception is a tight crop, which is why this is
        # overridable.
        auto_invert = mask.mean() > 0.65
        if auto_invert:
            info.notes.append(
                "the selected region covered most of the frame, so it was read "
                "as the background and inverted (--invert to override)"
            )
    else:
        auto_invert = bool(invert)

    if auto_invert:
        mask = ~mask
        info.inverted = True

    info.foreground_fraction = float(mask.mean())
    return mask, info


# ---------------------------------------------------------------------------
# Connectivity
# ---------------------------------------------------------------------------


def _label(mask: np.ndarray) -> tuple[np.ndarray, int]:
    """Label 4-connected components of a boolean mask.

    Two-pass union-find, vectorised per row-pair. scipy.ndimage.label does this
    faster and is not a dependency of this project; at 512x512 the difference is
    milliseconds.
    """
    height, width = mask.shape
    labels = np.zeros((height, width), dtype=np.int32)
    parent: list[int] = [0]

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for y in range(height):
        row = mask[y]
        up = labels[y - 1] if y > 0 else None
        left = 0
        for x in range(width):
            if not row[x]:
                left = 0
                continue
            above = int(up[x]) if up is not None else 0
            if left and above:
                labels[y, x] = left
                if left != above:
                    union(left, above)
            elif left:
                labels[y, x] = left
            elif above:
                labels[y, x] = above
                left = above
            else:
                parent.append(len(parent))
                labels[y, x] = parent[-1]
                left = parent[-1]
            left = labels[y, x]

    if len(parent) == 1:
        return labels, 0

    roots = np.array([find(i) for i in range(len(parent))], dtype=np.int32)
    unique = {root: index for index, root in enumerate(sorted(set(roots[1:])), start=1)}
    remap = np.zeros(len(parent), dtype=np.int32)
    for index, root in enumerate(roots):
        if index:
            remap[index] = unique[root]
    return remap[labels], len(unique)


def fill_enclosed(mask: np.ndarray) -> tuple[np.ndarray, int]:
    """Reclassify background regions that do not touch the frame as foreground.

    Returns the filled mask and the number of enclosed regions found. The
    regions themselves are recovered later as holes in the traced outline; this
    pass exists so the *outer* boundary is found by connectivity rather than by
    colour, which is what makes a photograph with a bright window in the middle
    of the subject trace correctly.
    """
    labels, count = _label(~mask)
    if count == 0:
        return mask.copy(), 0
    border = np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])
    outside = {int(v) for v in np.unique(border) if v}
    enclosed = [index for index in range(1, count + 1) if index not in outside]
    if not enclosed:
        return mask.copy(), 0
    filled = mask | np.isin(labels, enclosed)
    return filled, len(enclosed)


def largest_component(mask: np.ndarray) -> tuple[np.ndarray, int, float]:
    """Keep only the biggest connected blob.

    Returns the mask, how many components there were, and the fraction of
    foreground area that survived. A photo of a key holder on a wall often has a
    shadow, a screw or a stray highlight; the subject is the big one.
    """
    labels, count = _label(mask)
    if count <= 1:
        return mask.copy(), count, 1.0
    areas = np.bincount(labels.reshape(-1), minlength=count + 1)
    areas[0] = 0
    winner = np.argmax(areas).item()
    kept = labels == winner
    return kept, count, float(areas[winner] / max(1, areas.sum()))


def denoise(mask: np.ndarray, passes: int = 1) -> np.ndarray:
    """A 3x3 majority filter: kills salt-and-pepper noise before it becomes shape.

    A JPEG of a black object on a beige wall produces a few thousand isolated
    pixels on the wrong side of any threshold. They do not survive the smoothing
    that precedes the contour trace either, but they do reach the connectivity
    pass first, and a hundred one-pixel "regions" in the report is noise in the
    literal and the figurative sense.
    """
    result = mask
    for _ in range(max(0, passes)):
        padded = np.pad(result, 1, mode="edge").astype(np.uint8)
        neighbours = (
            padded[:-2, :-2] + padded[:-2, 1:-1] + padded[:-2, 2:]
            + padded[1:-1, :-2] + padded[1:-1, 1:-1] + padded[1:-1, 2:]
            + padded[2:, :-2] + padded[2:, 1:-1] + padded[2:, 2:]
        )
        result = neighbours >= 5
    return result


def smooth(mask: np.ndarray, passes: int = 2) -> np.ndarray:
    """A small box blur, producing the scalar field the contour tracer reads.

    Marching squares on a hard 0/1 grid produces a staircase; the same algorithm
    on a lightly blurred field interpolates the crossing and produces a contour
    that follows the pixel edge to a fraction of a pixel. Two passes of a 3x3
    box is the smallest amount that does it.
    """
    field_ = mask.astype(np.float64)
    for _ in range(max(0, passes)):
        padded = np.pad(field_, 1, mode="edge")
        field_ = (
            padded[:-2, :-2] + padded[:-2, 1:-1] + padded[:-2, 2:]
            + padded[1:-1, :-2] + padded[1:-1, 1:-1] + padded[1:-1, 2:]
            + padded[2:, :-2] + padded[2:, 1:-1] + padded[2:, 2:]
        ) / 9.0
    return field_


def read_mask(
    source: str | Path | bytes,
    *,
    threshold: float | None = None,
    invert: bool | None = None,
    max_dim: int = DEFAULT_MAX_DIM,
    keep_largest: bool = True,
) -> Mask:
    """Decode an image and reduce it to one clean foreground blob."""
    rgba, original = load_rgba(source, max_dim=max_dim)
    mask, info = foreground_mask(rgba, threshold=threshold, invert=invert)
    info.original_size = original
    info.traced_size = (int(mask.shape[1]), int(mask.shape[0]))

    mask = denoise(mask)

    # Two different questions, answered from two different masks. *Which blob is
    # the subject* is answered from the filled mask, because a van whose windows
    # read as background is still one van. *Where the openings are* is then
    # answered by putting those windows back inside the blob that won.
    filled, enclosed = fill_enclosed(mask)
    if keep_largest:
        kept, components, retained = largest_component(filled)
        if components > 1:
            info.notes.append(
                f"the image contained {components} separate regions; kept the "
                f"largest ({retained:.0%} of the traced area)"
            )
        filled = kept
    if enclosed:
        info.notes.append(
            f"{enclosed} enclosed region(s) treated as openings in the subject "
            "rather than as background"
        )
    filled = filled & mask

    info.foreground_fraction = float(filled.mean())
    if info.foreground_fraction < 0.005:
        raise ImageError(
            "almost nothing was selected as the subject "
            f"({info.foreground_fraction:.2%} of the image). Try --threshold to "
            "move the cut, or --invert if the subject is the lighter region."
        )

    # The tracer needs a border of background all the way round, or a subject
    # that runs off the edge of the frame produces an open contour.
    padded = np.pad(filled, 2, mode="constant", constant_values=False)
    touched = bool(
        filled[0].any() or filled[-1].any() or filled[:, 0].any() or filled[:, -1].any()
    )
    if touched:
        info.notes.append(
            "the subject touches the edge of the frame, so its outline was "
            "closed along that edge; crop the image if that is not what you want"
        )
    return Mask(array=padded, info=info)
