"""Emit a standalone build123d script for a planned key holder.

The traced outline arrives here as a few hundred (x, y) pairs, which is the one
place this project deliberately breaks its own "every dimension is a named
constant" rule: a polygon is data, not a parameter, and no slider can be
attached to its 287th vertex. Everything else -- plate thickness, hook
geometry, screw sizes, engraving depth -- is a named constant at the top of the
file, so the script that ships in the bundle is still the editable artifact the
rest of FormForge promises. Change `PLAQUE_T_MM` and re-run and you get the same
silhouette 2 mm thicker.

The script only ever needs build123d. It does not import FormForge, read the
image, or depend on anything that happened upstream, which is what lets it run
in the sandbox and, identically, on the user's own machine.
"""

from __future__ import annotations

import textwrap

from .design import HOOK_EMBED_MM, KeyHolderPlan

# A cut that stops exactly on the face it is cutting through leaves the kernel
# to decide whether two coincident faces are one face. Overshooting by a
# millimetre removes the question.
CUT_OVERSHOOT_MM = 1.0


def _num(value: float) -> str:
    """A dimension as a float literal: 7 is a count, 7.0 is a measurement."""
    rounded = round(float(value), 3)
    return f"{rounded:.1f}" if rounded == int(rounded) else f"{rounded:g}"


def _points(points: list[tuple[float, float]], indent: str = "    ") -> str:
    """Format a point list a few per line, so the file stays readable."""
    lines = []
    row: list[str] = []
    for x, y in points:
        row.append(f"({_num(x)}, {_num(y)})")
        if len(row) == 4:
            lines.append(indent + ", ".join(row) + ",")
            row = []
    if row:
        lines.append(indent + ", ".join(row) + ",")
    return "\n".join(lines)


def _polygon_list(rings: list[list[tuple[float, float]]]) -> str:
    if not rings:
        return "[]"
    blocks = []
    for ring in rings:
        blocks.append("    [\n" + _points(ring, indent="        ") + "\n    ],")
    return "[\n" + "\n".join(blocks) + "\n]"


def emit(plan: KeyHolderPlan, *, source_name: str = "an uploaded image") -> str:
    """The complete build123d script for this plan."""
    spec = plan.spec
    header = _header(plan, source_name)

    countersink_depth = max(0.8, (spec.keyhole_head_d_mm - spec.screw_d_mm) / 2.0)
    hooks = [_num(x) for x in plan.hook_x_mm]
    mounts = [f"({_num(m.x_mm)}, {_num(m.y_mm)})" for m in plan.mounts]

    parts = [
        header,
        "from build123d import *",
        "",
        "# -- dimensions ---------------------------------------------------------",
        f"PLAQUE_T_MM = {_num(plan.plaque_t_mm)}",
        f"ENGRAVE_DEPTH_MM = {_num(spec.engrave_depth_mm)}",
        f"CUT_OVERSHOOT_MM = {_num(CUT_OVERSHOOT_MM)}",
        f"HOOK_W_MM = {_num(spec.hook_w_mm)}",
        f"HOOK_BASE_Y_MM = {_num(plan.hook_base_y_mm)}",
        f"HOOK_EMBED_MM = {_num(HOOK_EMBED_MM)}",
        f"SCREW_D_MM = {_num(spec.screw_d_mm)}",
        f"KEYHOLE_HEAD_D_MM = {_num(spec.keyhole_head_d_mm)}",
        f"KEYHOLE_HEAD_H_MM = {_num(spec.keyhole_head_h_mm)}",
        f"KEYHOLE_SLOT_LEN_MM = {_num(spec.keyhole_slot_len_mm)}",
        f"KEYHOLE_LIP_MM = {_num(spec.keyhole_lip_mm)}",
        f"COUNTERSINK_D_MM = {_num(spec.keyhole_head_d_mm)}",
        f"COUNTERSINK_DEPTH_MM = {_num(countersink_depth)}",
        "",
        "# -- traced geometry ----------------------------------------------------",
        "# The outline is data rather than a parameter: it came from the image, and",
        "# the millimetre values below are the finished, manufacturable shape --",
        "# already scaled, already cleaned of anything too thin to print.",
        "OUTLINE_MM = [",
        _points(plan.outline),
        "]",
        "",
        f"HOLES_MM = {_polygon_list(plan.holes)}",
        "",
        f"ENGRAVE_MM = {_polygon_list(plan.engrave)}",
        "",
        f"HOOK_X_MM = [{', '.join(hooks)}]",
        "",
        "# The hook's side view as (out-from-wall, up-the-wall) offsets from the",
        "# front face of the plate. Every rising edge is 45 degrees or shallower,",
        "# which is what lets the whole part print with no supports.",
        "HOOK_PROFILE_MM = [",
        _points(plan.hook_profile),
        "]",
        "",
        f"MOUNTS_MM = [{', '.join(mounts)}]",
        "",
        "",
        _body(plan),
    ]
    return "\n".join(parts)


def _safe_name(name: str) -> str:
    """A caller-supplied filename, made safe to sit inside a docstring.

    The only string in this file that did not come from FormForge is the name of
    the uploaded image, and it goes into the header. A filename containing a
    triple quote would end the docstring early and turn the script into a
    syntax error the static gate then rejects -- a confusing way to be told that
    a file was called something odd.
    """
    cleaned = "".join(c for c in name if c.isprintable()).replace('"', "'").replace("\\", "/")
    cleaned = cleaned.strip()
    return (cleaned[:80] or "an uploaded image")


def _header(plan: KeyHolderPlan, source_name: str) -> str:
    width, height, depth = plan.bounding_box_mm()
    lines = [
        "Wall-mounted key holder, traced from " + _safe_name(source_name) + ".",
        "",
        f"Overall: {width:.0f} x {height:.0f} x {depth:.0f} mm, "
        f"{len(plan.hook_x_mm)} hook(s), {plan.mount} mount, "
        f"about {plan.estimated_mass_g:.0f} g of filament.",
        "",
        "PRINTING",
        "  Lay the flat back face on the bed. Every silhouette edge is then a",
        "  vertical wall and every hook face is 45 degrees or steeper, so the",
        "  whole part prints with no supports.",
        "  The layer lines run parallel to the wall, which means a hook root is",
        "  loaded across the layers rather than along them. That is the price of",
        "  an orientation that can print an arbitrary outline. For a rack that",
        "  will carry heavy bunches, print it in PETG with four perimeters.",
        "",
        "EDITING",
        "  Every dimension below is a named constant; the outline is a list of",
        "  points because a traced shape has no meaningful parameters. Change a",
        "  constant and re-run this file to rebuild the model:",
        "",
        "      pip install build123d && python source.py",
    ]
    if plan.notes:
        lines += ["", "NOTES"]
        for note in plan.notes:
            lines += textwrap.wrap(
                note, width=72, initial_indent="  - ", subsequent_indent="    "
            )
    if plan.warnings:
        lines += ["", "WARNINGS"]
        for warning in plan.warnings:
            lines += textwrap.wrap(
                warning, width=72, initial_indent="  ! ", subsequent_indent="    "
            )
    body = "\n".join(lines)
    return f'"""{body}\n"""\n'


def _body(plan: KeyHolderPlan) -> str:
    blocks = [
        "with BuildPart() as key_holder:",
        "    # The plate: silhouette and hook rail were unioned in 2D before they",
        "    # got here, so this is one profile and one extrusion rather than a",
        "    # boolean between a decorative shape and a bar.",
        "    with BuildSketch(Plane.XY) as plate:",
        "        with BuildLine():",
        "            Polyline(*OUTLINE_MM, close=True)",
        "        make_face()",
        "        for ring in HOLES_MM:",
        "            with BuildLine():",
        "                Polyline(*ring, close=True)",
        "            make_face(mode=Mode.SUBTRACT)",
        "    extrude(amount=PLAQUE_T_MM)",
        "",
    ]

    if plan.engrave:
        blocks += [
            "    # Interior detail from the image, engraved into the visible face",
            "    # rather than cut through it: a window in a car outline is a line,",
            "    # not a hole, and cutting it would leave the plate flexing.",
            "    with BuildSketch(Plane.XY.offset(PLAQUE_T_MM - ENGRAVE_DEPTH_MM)):",
            "        for ring in ENGRAVE_MM:",
            "            with BuildLine():",
            "                Polyline(*ring, close=True)",
            "            make_face()",
            "    extrude(amount=ENGRAVE_DEPTH_MM + CUT_OVERSHOOT_MM, mode=Mode.SUBTRACT)",
            "",
        ]

    if plan.hook_x_mm:
        blocks += [
            "    # One hook per position. The profile is drawn in the side view and",
            "    # extruded across, so the hook is a single solid with the plate at",
            "    # its root -- there is no join to fail where the load is highest.",
            "    for hook_x in HOOK_X_MM:",
            "        with BuildSketch(Plane.YZ.offset(hook_x)):",
            "            with BuildLine():",
            "                Polyline(",
            "                    *[",
            "                        (HOOK_BASE_Y_MM + up, PLAQUE_T_MM + out)",
            "                        for out, up in HOOK_PROFILE_MM",
            "                    ],",
            "                    close=True,",
            "                )",
            "            make_face()",
            "        extrude(amount=HOOK_W_MM / 2, both=True)",
            "",
        ]

    if plan.mounts and plan.mount == "keyhole":
        blocks += [
            "    # Keyhole hangers, cut into the back. The screw head goes in through",
            "    # the round opening and the piece drops onto the slot above it, so",
            "    # the lip left against the wall is what carries the weight.",
            "    for mount_x, mount_y in MOUNTS_MM:",
            "        with BuildSketch(Plane.XY):",
            "            with Locations((mount_x, mount_y)):",
            "                Circle(KEYHOLE_HEAD_D_MM / 2)",
            "            with Locations((mount_x, mount_y + KEYHOLE_SLOT_LEN_MM / 2)):",
            "                Rectangle(SCREW_D_MM, KEYHOLE_SLOT_LEN_MM)",
            "        extrude(amount=KEYHOLE_LIP_MM, both=True, mode=Mode.SUBTRACT)",
            "",
            "        # The cavity behind the lip, wide enough for the head along the",
            "        # whole of its travel. Drawn as one slot rather than as two",
            "        # circles unioned with a rectangle: that union is tangent at",
            "        # four points, and a tangency is where OCCT produces an",
            "        # almost-valid solid with a boundary edge in it.",
            "        with BuildSketch(Plane.XY.offset(KEYHOLE_LIP_MM)):",
            "            with Locations((mount_x, mount_y + KEYHOLE_SLOT_LEN_MM / 2)):",
            "                SlotCenterToCenter(",
            "                    KEYHOLE_SLOT_LEN_MM, KEYHOLE_HEAD_D_MM, rotation=90",
            "                )",
            "        extrude(amount=KEYHOLE_HEAD_H_MM, mode=Mode.SUBTRACT)",
            "",
        ]
    elif plan.mounts and plan.mount == "screw":
        blocks += [
            "    # Countersunk screw holes, straight through, with the cone on the",
            "    # visible face so the head finishes flush.",
            "    for mount_x, mount_y in MOUNTS_MM:",
            "        with BuildSketch(Plane.XY):",
            "            with Locations((mount_x, mount_y)):",
            "                Circle(SCREW_D_MM / 2)",
            "        extrude(amount=PLAQUE_T_MM + CUT_OVERSHOOT_MM, both=True, "
            "mode=Mode.SUBTRACT)",
            "        with Locations(",
            "            (mount_x, mount_y, PLAQUE_T_MM - COUNTERSINK_DEPTH_MM)",
            "        ):",
            "            Cone(",
            "                bottom_radius=SCREW_D_MM / 2,",
            "                top_radius=COUNTERSINK_D_MM / 2,",
            "                height=COUNTERSINK_DEPTH_MM,",
            "                align=(Align.CENTER, Align.CENTER, Align.MIN),",
            "                mode=Mode.SUBTRACT,",
            "            )",
            "",
        ]

    blocks.append("result = key_holder.part")
    return "\n".join(blocks)
