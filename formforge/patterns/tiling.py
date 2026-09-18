"""Splitting a panel into tiles that fit on the plate.

The arithmetic is not the interesting part; the conventions are, because they
are what let a tile be built without knowing anything about its neighbours:

* **Uniform tiles.** Every tile is exactly `panel / cols` by `panel / rows`. A
  seam is then the same length from both sides, so both tiles put their joints
  in the same places by computing the same thing rather than by being told.
* **Male right and top, female left and bottom.** Every interior seam gets
  exactly one of each, with no negotiation.
* **Row A at the top.** Tiles are labelled the way someone standing in front of
  the wall reads them -- A1 top left -- not the way the coordinate system runs.
  The whole point of the label is to be used by a person holding the tile.

Fitting is checked against the printer's build volume *including* whatever the
joint adds: a 250 mm tile with 9 mm dovetails is a 259 mm object, and finding
that out in the slicer is finding it out too late.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .field import Bounds

__all__ = ["TilePlan", "TileSpec", "plan_panel", "row_label"]

# Margin left around the tile on the plate. Skirts, brims and the fact that
# nobody's first layer is perfect at the very edge of the bed.
DEFAULT_PLATE_MARGIN_MM = 10.0


def row_label(index: int) -> str:
    """A, B, ... Z, AA, AB, ... for row indices."""
    label = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        label = chr(ord("A") + remainder) + label
    return label


@dataclass(frozen=True)
class TileSpec:
    """One tile: where it is on the panel and which of its edges are seams."""

    row: int
    col: int
    label: str
    bounds: Bounds
    seams: dict[str, bool]
    polarity: dict[str, str]

    @property
    def rect(self) -> tuple[float, float, float, float]:
        return (self.bounds.x0, self.bounds.y0, self.bounds.x1, self.bounds.y1)

    @property
    def is_interior(self) -> bool:
        return all(self.seams.values())

    def neighbours(self) -> dict[str, str]:
        """Edge -> the label of the tile across that seam, for the assembly map."""
        out = {}
        if self.seams.get("left"):
            out["left"] = f"{row_label(self.row)}{self.col}"
        if self.seams.get("right"):
            out["right"] = f"{row_label(self.row)}{self.col + 2}"
        if self.seams.get("top"):
            out["top"] = f"{row_label(self.row - 1)}{self.col + 1}"
        if self.seams.get("bottom"):
            out["bottom"] = f"{row_label(self.row + 1)}{self.col + 1}"
        return out


@dataclass(frozen=True)
class TilePlan:
    """The whole grid."""

    panel: Bounds
    rows: int
    cols: int
    tiles: tuple[TileSpec, ...]
    tile_w_mm: float
    tile_h_mm: float
    joint_growth_mm: float = 0.0

    @property
    def count(self) -> int:
        return len(self.tiles)

    @property
    def footprint_mm(self) -> tuple[float, float]:
        """The largest a single tile gets once its joint is added."""
        return (self.tile_w_mm + self.joint_growth_mm, self.tile_h_mm + self.joint_growth_mm)

    def tile(self, label: str) -> TileSpec:
        for spec in self.tiles:
            if spec.label == label.upper():
                return spec
        raise KeyError(
            f"no tile {label!r} in this panel; labels run A1 to "
            f"{row_label(self.rows - 1)}{self.cols}"
        )

    def ascii_map(self) -> str:
        """The grid as text, for the assembly instructions."""
        width = max(len(t.label) for t in self.tiles) + 2
        lines = []
        for row in range(self.rows):
            cells = [f"{row_label(row)}{col + 1}".center(width) for col in range(self.cols)]
            lines.append("|" + "|".join(cells) + "|")
            lines.append("+" + "+".join("-" * width for _ in range(self.cols)) + "+")
        return "+" + "+".join("-" * width for _ in range(self.cols)) + "+\n" + "\n".join(lines)


def plan_panel(
    width_mm: float,
    height_mm: float,
    *,
    build_volume_mm: tuple[float, float, float],
    rows: int | None = None,
    cols: int | None = None,
    max_tile_mm: float | None = None,
    plate_margin_mm: float = DEFAULT_PLATE_MARGIN_MM,
    joint_growth_mm: float = 0.0,
) -> TilePlan:
    """Work out the grid, or check the one that was asked for.

    With `rows`/`cols` unset the grid is the smallest one whose tiles fit the
    plate. Smallest, not squarest: every extra tile is another seam to hide and
    another hour of printing, so the default is as few pieces as the machine
    allows.
    """
    if width_mm <= 0 or height_mm <= 0:
        raise ValueError("panel width and height must be positive")

    plate_x = build_volume_mm[0] - plate_margin_mm
    plate_y = build_volume_mm[1] - plate_margin_mm
    usable_x = max(plate_x - joint_growth_mm, 10.0)
    usable_y = max(plate_y - joint_growth_mm, 10.0)
    if max_tile_mm:
        usable_x = min(usable_x, max_tile_mm)
        usable_y = min(usable_y, max_tile_mm)

    resolved_cols = int(cols) if cols else max(1, math.ceil(width_mm / usable_x - 1e-9))
    resolved_rows = int(rows) if rows else max(1, math.ceil(height_mm / usable_y - 1e-9))
    if resolved_cols < 1 or resolved_rows < 1:
        raise ValueError("rows and cols must be at least 1")

    tile_w = width_mm / resolved_cols
    tile_h = height_mm / resolved_rows
    if tile_w + joint_growth_mm > plate_x + 1e-6 or tile_h + joint_growth_mm > plate_y + 1e-6:
        raise ValueError(
            f"a {tile_w:.0f} x {tile_h:.0f} mm tile plus {joint_growth_mm:.0f} mm of joint "
            f"does not fit a {build_volume_mm[0]:.0f} x {build_volume_mm[1]:.0f} mm plate "
            f"with a {plate_margin_mm:.0f} mm margin. Ask for more tiles, a smaller panel, "
            f"or a different printer profile."
        )

    panel = Bounds.sized(width_mm, height_mm)
    tiles = []
    for row in range(resolved_rows):
        for col in range(resolved_cols):
            x0 = panel.x0 + col * tile_w
            # Row 0 is the top of the panel, so it takes the highest Y.
            y1 = panel.y1 - row * tile_h
            seams = {
                "left": col > 0,
                "right": col < resolved_cols - 1,
                "top": row > 0,
                "bottom": row < resolved_rows - 1,
            }
            tiles.append(
                TileSpec(
                    row=row,
                    col=col,
                    label=f"{row_label(row)}{col + 1}",
                    bounds=Bounds(x0, y1 - tile_h, x0 + tile_w, y1),
                    seams=seams,
                    polarity={
                        "right": "male",
                        "top": "male",
                        "left": "female",
                        "bottom": "female",
                    },
                )
            )

    return TilePlan(
        panel=panel,
        rows=resolved_rows,
        cols=resolved_cols,
        tiles=tuple(tiles),
        tile_w_mm=tile_w,
        tile_h_mm=tile_h,
        joint_growth_mm=joint_growth_mm,
    )
