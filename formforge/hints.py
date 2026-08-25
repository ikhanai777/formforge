"""Translate kernel failures into something a model can act on (spec section 6.1).

OCCT reports most failures as `StdFail_NotDone` with no context, which tells a
repair attempt nothing at all. Mapping those to a plain-English cause roughly
doubles the repair success rate, so this table is load-bearing product logic
rather than cosmetics.

Add a row here every time a real generation fails in a new way. The table is the
accumulated debugging knowledge of the system.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class HintRule:
    """One traceback pattern and the advice it maps to."""

    # Regex matched case-insensitively against the exception text + traceback.
    pattern: str
    error_class: str
    hint: str
    # Optional: which loop phase this failure belongs to, when the raw phase is
    # ambiguous.
    phase: str | None = None


# Ordered: the first match wins, so specific patterns precede general ones.
HINT_RULES: tuple[HintRule, ...] = (
    # -- fillet and chamfer ------------------------------------------------
    HintRule(
        r"fillet.*(?:failed|not\s*done|error)|ChFi3d|BRepFilletAPI",
        "FilletFailure",
        "A fillet or chamfer failed. The radius is almost certainly larger than "
        "the smallest adjacent face. Halve the radius, or select fewer edges: "
        "fillet only the edges you actually need rounded rather than the whole "
        "solid. A fillet radius must be strictly less than half the thinnest "
        "wall it touches.",
    ),
    HintRule(
        r"chamfer.*(?:failed|not\s*done)",
        "ChamferFailure",
        "A chamfer failed, usually because the chamfer distance exceeds the "
        "length of an adjacent edge. Reduce the distance or narrow the edge "
        "selection.",
    ),
    # -- booleans ----------------------------------------------------------
    HintRule(
        r"BOPAlgo|BRepAlgoAPI|boolean.*(?:failed|not\s*done)",
        "BooleanFailure",
        "A boolean operation failed. Common causes: the two solids are exactly "
        "coplanar on a face (offset one by 0.01 mm so the surfaces properly "
        "intersect), the tools do not actually overlap, or one operand is not a "
        "closed solid. Check that each operand has positive volume before "
        "combining them.",
    ),
    HintRule(
        r"(?:offset|shell|thicken).*(?:failed|not\s*done)|BRepOffset",
        "OffsetFailure",
        "An offset/shell operation failed. Shelling collapses when the wall "
        "thickness exceeds the local radius of curvature -- a 2 mm shell cannot "
        "fit inside a 1.5 mm fillet. Reduce the shell thickness, remove fillets "
        "applied before the shell, or shell first and fillet afterwards.",
    ),
    # -- sketches and wires ------------------------------------------------
    HintRule(
        r"wire.*(?:not\s*closed|open)|BRepBuilderAPI_NotDone|make_face",
        "OpenWireFailure",
        "A face could not be built because its wire is not closed. Every sketch "
        "contour must start and end at the same point. If you built the profile "
        "from line segments, confirm the last point equals the first exactly, "
        "and that no two segments are duplicated or zero-length.",
    ),
    HintRule(
        r"self.?intersect",
        "SelfIntersection",
        "The profile or the resulting solid self-intersects. A swept or lofted "
        "path that turns tighter than the profile's own width will fold through "
        "itself. Increase the turn radius or shrink the profile.",
    ),
    # -- extrude / revolve / loft -----------------------------------------
    HintRule(
        r"extrude.*(?:failed|zero|empty)|Prism",
        "ExtrudeFailure",
        "The extrude produced nothing. Either the sketch is empty (check that "
        "the sketch context actually captured your shapes) or the extrude "
        "amount is zero.",
    ),
    HintRule(
        r"revolve.*(?:failed|not\s*done)|Revol",
        "RevolveFailure",
        "The revolve failed. A profile that crosses the axis of revolution "
        "produces an invalid solid -- keep the entire profile on one side of "
        "the axis, touching it at most along an edge.",
    ),
    HintRule(
        r"loft.*(?:failed|not\s*done)|ThruSections",
        "LoftFailure",
        "The loft failed. Lofted sections must have compatible orientation and "
        "vertex counts. Use the same primitive for each section where possible, "
        "and make sure the sections do not sit at the same Z.",
    ),
    HintRule(
        r"sweep.*(?:failed|not\s*done)|PipeShell",
        "SweepFailure",
        "The sweep failed. The path turns too sharply for the profile width, or "
        "the profile is not perpendicular to the start of the path. Add a "
        "fillet to the path corners with radius greater than half the profile "
        "width.",
    ),
    # -- text --------------------------------------------------------------
    HintRule(
        r"font|freetype|Text\(",
        "FontFailure",
        "Text creation failed, usually an unavailable font. Use a common "
        "sans-serif family name and let the toolkit fall back, or omit the font "
        "argument entirely. Do not pass a font file path -- the sandbox has no "
        "access to host fonts.",
    ),
    # -- empty / null results ---------------------------------------------
    HintRule(
        r"null\s*shape|empty\s*(?:shape|compound|part)|has\s*no\s*volume",
        "EmptyResult",
        "The script finished but produced no geometry. Check that the final "
        "variable holds a solid, that a builder context was not left empty, and "
        "that a subtraction did not remove everything.",
    ),
    HintRule(
        r"StdFail_NotDone",
        "KernelNotDone",
        "The CAD kernel refused an operation without saying why -- almost always "
        "a fillet radius, shell thickness, or offset distance that does not fit "
        "the local geometry. Rebuild the shape with those values halved to "
        "identify which one is at fault, then pick the largest value that works.",
    ),
    # -- script-level errors ----------------------------------------------
    HintRule(
        r"NameError.*name '(\w+)'",
        "NameError",
        "The script references a name that was never defined. Every dimension "
        "must be declared as a module-level constant before it is used.",
    ),
    HintRule(
        r"TypeError.*(?:positional|argument)",
        "SignatureError",
        "A call was made with the wrong arguments. Check the API cheat-sheet: "
        "build123d primitives take dimensions positionally (Box(length, width, "
        "height)) and modifiers by keyword.",
    ),
    HintRule(
        r"AttributeError.*'(\w+)' object has no attribute",
        "AttributeError",
        "An attribute or method does not exist on that object. This usually "
        "means mixing the algebra API with the builder API -- pick one style and "
        "use it consistently through the script.",
    ),
    HintRule(
        r"ZeroDivisionError",
        "ZeroDivision",
        "A division by zero, typically from a parameter that defaulted to 0. "
        "Guard any parameter used as a divisor with a minimum value.",
    ),
    HintRule(
        r"RecursionError|maximum recursion",
        "RecursionLimit",
        "The script recursed without a base case. Geometry generation should be "
        "iterative over a bounded range, not recursive.",
    ),
    HintRule(
        r"MemoryError|Cannot allocate",
        "OutOfMemory",
        "The script exhausted memory, usually from a pattern with far too many "
        "instances or a tessellation deflection set far too fine. Reduce the "
        "instance count, or coarsen linear_deflection.",
    ),
)

_COMPILED: tuple[tuple[re.Pattern[str], HintRule], ...] = tuple(
    (re.compile(rule.pattern, re.IGNORECASE | re.DOTALL), rule) for rule in HINT_RULES
)

GENERIC_HINT = (
    "The script raised an error the hint table does not recognise. Read the "
    "traceback line by line, identify the operation that failed, and rebuild "
    "that step more conservatively -- smaller radii, explicit intermediate "
    "variables, and a volume check after each boolean."
)


def classify(text: str) -> tuple[str, str]:
    """Map an exception message plus traceback to (error_class, hint).

    Matching is over the concatenated message and traceback, so a bare
    `StdFail_NotDone` still picks up the operation name from the calling frame.
    """
    haystack = text or ""
    for pattern, rule in _COMPILED:
        if pattern.search(haystack):
            return rule.error_class, rule.hint
    # Fall back to the exception class name if the traceback has one.
    match = re.search(r"^(\w+(?:Error|Exception|Failure)):", haystack, re.MULTILINE)
    error_class = match.group(1) if match else "UnknownError"
    return error_class, GENERIC_HINT


def hint_for(exc: BaseException, traceback_text: str = "") -> tuple[str, str]:
    """Classify a live exception object."""
    combined = f"{type(exc).__name__}: {exc}\n{traceback_text}"
    error_class, hint = classify(combined)
    if error_class == "UnknownError":
        error_class = type(exc).__name__
    return error_class, hint
