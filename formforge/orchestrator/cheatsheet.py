"""The build123d API cheat-sheet: a stable, cached prompt fragment.

Roughly the sixty operations that cover the overwhelming majority of functional,
dimensioned parts (spec section 6.3). This is not documentation -- it is a
prompt, and every line earns its place by preventing a specific failure the
repair loop would otherwise have to catch.

The failures it is written against, in order of how often they happen:

1. Mixing the builder and algebra APIs in one script. Both are valid; combining
   them produces `AttributeError` on objects that look like they should work.
2. Selecting faces and edges positionally after an operation that changed the
   count -- the single most common way a script that worked at one parameter
   value breaks at another.
3. Fillet and shell radii that do not fit the geometry they are applied to.
4. Boolean operands that touch on a coplanar face instead of overlapping, which
   produces two disconnected solids rather than one.

Keep this byte-stable. It sits in the cached prefix of every codegen call, and
editing it invalidates the cache for every request until it warms again.
"""

from __future__ import annotations

CHEATSHEET = '''\
BUILD123D API CHEAT-SHEET

Import once, at the top:

    from build123d import *

STRUCTURE OF EVERY SCRIPT

    from build123d import *

    WIDTH_MM = 60.0          # every dimension is a named module constant
    HEIGHT_MM = 30.0

    with BuildPart() as part:
        ...                  # build here
    result = part.part       # assign the finished SOLID to `result`

`result` must be a solid with positive volume. Not a sketch, not a builder.

PICK ONE API AND STAY IN IT

Builder mode (recommended -- use this):

    with BuildPart() as part:
        with BuildSketch() as plan:
            Rectangle(WIDTH_MM, DEPTH_MM)
        extrude(amount=HEIGHT_MM)
    result = part.part

Algebra mode (also valid, but do not mix the two in one script):

    result = Box(WIDTH_MM, DEPTH_MM, HEIGHT_MM) - Cylinder(HOLE_R_MM, HEIGHT_MM)

Mixing them is the most common cause of AttributeError in generated scripts.

3D PRIMITIVES (algebra mode, or inside BuildPart)

    Box(length, width, height, align=(Align.CENTER, Align.CENTER, Align.MIN))
    Cylinder(radius, height)
    Sphere(radius)
    Cone(bottom_radius, top_radius, height)
    Torus(major_radius, minor_radius)
    Wedge(xsize, ysize, zsize, xmin, zmin, xmax, zmax)

Align controls where the origin sits: Align.MIN, Align.CENTER, Align.MAX per
axis. `align=(Align.CENTER, Align.CENTER, Align.MIN)` puts a box on the plate.

2D SHAPES (inside BuildSketch)

    Rectangle(width, height, align=...)
    RectangleRounded(width, height, radius)
    Circle(radius)
    Ellipse(x_radius, y_radius)
    RegularPolygon(radius, side_count, major_radius=False)   # False = across flats
    Polygon((x1, y1), (x2, y2), ...)
    SlotOverall(width, height)
    Text(txt, font_size=CAP_MM)                              # font_size is CAP HEIGHT
    Triangle(a=..., b=..., c=...)

SKETCH TO SOLID

    extrude(amount=H)                       # along the sketch plane's normal
    extrude(amount=H, both=True)            # symmetric, H each way
    extrude(amount=H, mode=Mode.SUBTRACT)   # cut instead of add
    revolve(axis=Axis.Z, revolution_arc=360)
    loft()                                  # between two or more sketches
    sweep(path=...)

Modes: Mode.ADD (default), Mode.SUBTRACT, Mode.INTERSECT, Mode.REPLACE.

PLANES AND POSITIONS

    Plane.XY, Plane.XZ, Plane.YZ            # Plane.XZ's normal is -Y
    Plane.XY.offset(Z_MM)                   # moved along its own normal
    BuildSketch(some_face)                  # sketch directly on a face
    with Locations((x, y), (x2, y2)):       # repeat the next shape at each point
        Circle(R_MM)
    Pos(x, y, z) * shape                    # translate (algebra mode)
    Rot(rx, ry, rz) * shape                 # rotate (algebra mode)

MODIFIERS

    fillet(edges, radius=R_MM)
    chamfer(edges, length=L_MM)
    offset(amount=T_MM, kind=Kind.INTERSECTION)
    mirror(about=Plane.YZ)
    scale(by=FACTOR)

SELECTING FACES AND EDGES -- READ THIS TWICE

Select by geometry, never by a bare index. An index that is correct at one
parameter value silently selects a different face at another, and the script
appears to work until someone changes a slider.

    part.faces().filter_by(Plane.XY)              # faces parallel to XY
    part.faces().filter_by(Plane.XY).sort_by(Axis.Z)[-1]     # topmost
    part.faces().filter_by(Plane.XY).sort_by(Axis.Z)[0]      # bottom
    part.faces().filter_by(GeomType.CYLINDER)     # cylindrical faces
    part.edges().group_by(Axis.Z)[0]              # all edges at the lowest Z
    part.edges().group_by(Axis.Z)[-1]             # all edges at the highest Z
    part.edges().filter_by(Axis.X)                # edges running along X
    some_face.edges()                             # the edges bounding one face

`group_by` returns groups of equal position, which is what you almost always
want for "every edge round the base". `sort_by` returns a flat sorted list.

HOLLOWING

Prefer cutting a cavity to using shell(): a cavity is one subtract and always
works; shell fails whenever the thickness exceeds a local radius of curvature.

    with BuildSketch(Plane.XY.offset(FLOOR_MM)) as cavity:
        RectangleRounded(OUTER_L_MM - 2*WALL_MM, OUTER_W_MM - 2*WALL_MM, INNER_R_MM)
    extrude(amount=HEIGHT_MM, mode=Mode.SUBTRACT)

Match the inner corner radius to the outer one minus the wall, or the wall
thickens at the corners.

RULES THAT PREVENT THE COMMON FAILURES

- Two solids that merely touch on a coplanar face do NOT union into one solid.
  Overlap them by at least 0.01 mm, or build them as a single profile.
- A fillet radius must be strictly less than half the thinnest wall it touches.
  If a fillet fails, halve it before trying anything else.
- Chamfer or fillet the outer form BEFORE cutting holes into it. Doing it after
  catches the hole rims too.
- A sketch wire must close. Every profile starts and ends at the same point.
- Keep a revolve profile entirely on one side of its axis.
- Assert nothing; just make the geometry correct. The validator checks the
  bounding box for you.

TEXT

    with BuildSketch(part.faces().filter_by(Plane.XY).sort_by(Axis.Z)[-1]) as label:
        Text(TEXT, font_size=CAP_H_MM)
    extrude(amount=EMBOSS_MM)                       # raised
    extrude(amount=-DEPTH_MM, mode=Mode.SUBTRACT)   # engraved

Do not pass font_path -- the sandbox has no access to host fonts. Omit `font`
entirely, or use a common family name and let it fall back.

Text on a face is oriented by that face's plane. Sketching on the top face gives
text that reads correctly from above. If you sketch on a face whose normal
points along -X or -Y, the text will be mirrored: mirror it in the sketch.

AVAILABLE MODULES

build123d, math, numpy. Nothing else -- no os, no file access, no network. The
script runs in a sandbox with a 30 second CPU limit.
'''


def cheatsheet() -> str:
    """The cheat-sheet. Byte-stable so it stays in the prompt cache."""
    return CHEATSHEET


def registry_summary(templates: list) -> str:
    """A compact catalogue of the registry, for the codegen prompt.

    Included so the model knows what already exists and can say "that is a
    gridfinity bin" rather than reinventing one badly. Sorted by id so the text
    is identical between calls with the same registry -- an unsorted iteration
    here would silently break the cache prefix on every request.
    """
    if not templates:
        return ""
    lines = ["EXISTING TEMPLATES (prefer these; they are verified and parameterised)"]
    for template in sorted(templates, key=lambda t: t.id):
        tags = ", ".join(template.tags[:4])
        lines.append(f"  {template.id} [{template.category}] -- {template.display_name}")
        if tags:
            lines.append(f"      {tags}")
    return "\n".join(lines)
