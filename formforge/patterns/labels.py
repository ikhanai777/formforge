"""A 5 x 7 font, for the one string that has to be on every tile.

Nine tiles of a dune field look almost identical face down on a table, and the
one thing that turns a two-hour assembly into a ten-minute one is knowing that
this tile is B2 and that its arrow points up. So every tile carries its grid
reference and an orientation arrow, recessed into the back where they cannot be
seen once it is on the wall.

A bitmap font rather than a real one, because the alternative is a font
dependency and a glyph-outline pipeline for the sixteen characters this needs.
Rectangular pixels are also the most printable letterform there is: every wall
is vertical, every floor is flat, and a 0.6 mm deep recess in a 3 mm back reads
cleanly off a 0.4 mm nozzle at any size above about 6 mm cap height.

The text is mirrored in X by default. It is being cut into a face that is
looked at from -Z, so an unmirrored string reads backwards exactly when you
need it -- while the tiles are face down and you are trying to sort them.
"""

from __future__ import annotations

from shapely import affinity
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

__all__ = ["GLYPHS", "arrow_polygon", "text_polygon", "text_width"]

# Rows run top to bottom, so the tables below read the way the glyph looks.
GLYPHS: dict[str, tuple[str, ...]] = {
    "A": (".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "B": ("####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."),
    "C": (".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."),
    "D": ("####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."),
    "E": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "F": ("#####", "#....", "#....", "####.", "#....", "#....", "#...."),
    "G": (".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".###."),
    "H": ("#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "I": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "#####"),
    "J": ("..###", "...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."),
    "K": ("#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"),
    "L": ("#....", "#....", "#....", "#....", "#....", "#....", "#####"),
    "M": ("#...#", "##.##", "#.#.#", "#...#", "#...#", "#...#", "#...#"),
    "N": ("#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#", "#...#"),
    "O": (".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "P": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
    "Q": (".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"),
    "R": ("####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"),
    "S": (".####", "#....", "#....", ".###.", "....#", "....#", "####."),
    "T": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "U": ("#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "V": ("#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."),
    "W": ("#...#", "#...#", "#...#", "#...#", "#.#.#", "##.##", "#...#"),
    "X": ("#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"),
    "Y": ("#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."),
    "Z": ("#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"),
    "0": (".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."),
    "1": ("..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "2": (".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"),
    "3": ("#####", "...#.", "..#..", "...#.", "....#", "#...#", ".###."),
    "4": ("...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."),
    "5": ("#####", "#....", "####.", "....#", "....#", "#...#", ".###."),
    "6": ("..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."),
    "7": ("#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."),
    "8": (".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."),
    "9": (".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."),
    "-": (".....", ".....", ".....", "#####", ".....", ".....", "....."),
    ".": (".....", ".....", ".....", ".....", ".....", ".##..", ".##.."),
    " ": (".....", ".....", ".....", ".....", ".....", ".....", "....."),
}

_GLYPH_COLS = 5
_GLYPH_ROWS = 7
_TRACKING = 1  # blank columns between glyphs


def text_width(text: str, height_mm: float) -> float:
    """Width of a rendered string, in the same units as its cap height."""
    pixel = height_mm / _GLYPH_ROWS
    n = len(text)
    if n == 0:
        return 0.0
    return pixel * (n * _GLYPH_COLS + (n - 1) * _TRACKING)


def text_polygon(
    text: str,
    *,
    height_mm: float,
    center: tuple[float, float] = (0.0, 0.0),
    mirror: bool = True,
) -> Polygon | None:
    """Render a string as one polygon, centred on `center`.

    Unknown characters are dropped rather than raising: a tile label is a
    convenience, and refusing to build a 600 mm panel because someone put a
    slash in a label would be the wrong trade.
    """
    pixel = height_mm / _GLYPH_ROWS
    boxes = []
    cursor = 0.0
    for character in text.upper():
        rows = GLYPHS.get(character)
        if rows is None:
            cursor += pixel * (_GLYPH_COLS + _TRACKING)
            continue
        for row_index, row in enumerate(rows):
            for col_index, cell in enumerate(row):
                if cell != "#":
                    continue
                x0 = cursor + col_index * pixel
                # Row 0 is the top of the glyph, so Y counts down from the cap.
                y0 = (_GLYPH_ROWS - 1 - row_index) * pixel
                # Overlap each pixel very slightly so that diagonal neighbours
                # merge into one polygon instead of meeting at a single point,
                # which is a self-touching ring no extruder will accept.
                boxes.append(
                    box(
                        x0 - pixel * 0.01,
                        y0 - pixel * 0.01,
                        x0 + pixel * 1.01,
                        y0 + pixel * 1.01,
                    )
                )
        cursor += pixel * (_GLYPH_COLS + _TRACKING)

    if not boxes:
        return None
    glyphs = unary_union(boxes)
    width = text_width(text, height_mm)
    glyphs = affinity.translate(glyphs, xoff=-width / 2.0, yoff=-height_mm / 2.0)
    if mirror:
        glyphs = affinity.scale(glyphs, xfact=-1.0, yfact=1.0, origin=(0.0, 0.0))
    return affinity.translate(glyphs, xoff=center[0], yoff=center[1])


def arrow_polygon(
    *,
    height_mm: float,
    center: tuple[float, float] = (0.0, 0.0),
    mirror: bool = True,
) -> Polygon:
    """An upward arrow: which way is up when the tile is face down."""
    h = height_mm
    w = height_mm * 0.62
    shaft = w * 0.30
    points = [
        (-w / 2, h * 0.15),
        (0.0, h * 0.5),
        (w / 2, h * 0.15),
        (shaft / 2, h * 0.15),
        (shaft / 2, -h * 0.5),
        (-shaft / 2, -h * 0.5),
        (-shaft / 2, h * 0.15),
    ]
    arrow = Polygon(points)
    if mirror:
        arrow = affinity.scale(arrow, xfact=-1.0, yfact=1.0, origin=(0.0, 0.0))
    return affinity.translate(arrow, xoff=center[0], yoff=center[1])
