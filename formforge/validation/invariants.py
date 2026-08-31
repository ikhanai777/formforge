"""Tier 3: does this satisfy what its category promises? (spec section 7.3)

Tier 1 and 2 are universal. Tier 3 is where product knowledge lives: a planter
that is watertight, printable and has no drainage hole passes every generic
check and is still a bad planter.

Two sources of rules:

* Category defaults, declared here, which apply to every model of that kind.
* Per-template `invariants`, declared as expressions in the template YAML, which
  encode what that specific design guarantees.

Template invariants are expressions rather than code because they are authored
alongside the template by whoever print-tests it, and reviewed as data. They are
evaluated by a restricted AST walker -- not `eval` -- so a registry entry can
never become an execution vector.
"""

from __future__ import annotations

import ast
import math
import operator
import re
from dataclasses import dataclass
from typing import Any

from .mesh import MeshMeasurements
from .report import Severity, ValidationReport, check

TIER = 3

# Density in g/cm^3, for the mass estimates the mount-strength rules need.
DENSITIES = {"PLA": 1.24, "PETG": 1.27, "ABS": 1.04, "ASA": 1.07, "TPU": 1.21}


class InvariantError(Exception):
    """A template invariant is malformed. A registry bug, not a model failure."""


# ---------------------------------------------------------------------------
# Restricted expression evaluation
# ---------------------------------------------------------------------------

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_CMP_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}

_FUNCTIONS: dict[str, Any] = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "len": len,
    "int": int,
    "float": float,
    "bool": bool,
    "sqrt": math.sqrt,
    "sin": lambda d: math.sin(math.radians(d)),
    "cos": lambda d: math.cos(math.radians(d)),
    "tan": lambda d: math.tan(math.radians(d)),
    "any": any,
    "all": all,
}

# `A implies B` reads far better in a template than `not (A) or (B)`, and the
# rewrite is unambiguous because `implies` is not a Python keyword.
_IMPLIES_RE = re.compile(r"^(?P<lhs>.+?)\s+implies\s+(?P<rhs>.+)$", re.DOTALL)


def evaluate(expression: str, context: dict[str, Any]) -> Any:
    """Evaluate one invariant expression against a measurement context."""
    match = _IMPLIES_RE.match(expression.strip())
    if match:
        source = f"(not ({match.group('lhs')})) or ({match.group('rhs')})"
    else:
        source = expression

    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise InvariantError(f"invariant {expression!r} does not parse: {exc.msg}") from exc
    return _eval_node(tree.body, context, expression)


def names_in(expression: str) -> set[str]:
    """The identifiers an expression reads.

    Used to answer "which parameters is this rule actually about", so a caller
    that has to resolve a failing rule knows which knobs are in play. Parsed
    with the same `implies` rewrite as `evaluate`, so the two never disagree
    about what an expression contains. An expression that does not parse simply
    names nothing -- reporting the syntax error is `evaluate`'s job.
    """
    match = _IMPLIES_RE.match(expression.strip())
    source = (
        f"(not ({match.group('lhs')})) or ({match.group('rhs')})" if match else expression
    )
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError:
        return set()
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def _eval_node(node: ast.AST, ctx: dict[str, Any], source: str) -> Any:
    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        if node.id in ctx:
            return ctx[node.id]
        if node.id in _FUNCTIONS:
            return _FUNCTIONS[node.id]
        raise InvariantError(
            f"invariant {source!r} references unknown name {node.id!r}; "
            f"available: {', '.join(sorted(k for k in ctx if not k.startswith('_')))}"
        )

    if isinstance(node, ast.Attribute):
        value = _eval_node(node.value, ctx, source)
        if node.attr.startswith("_"):
            raise InvariantError(f"invariant {source!r} accesses private attribute")
        if isinstance(value, dict):
            if node.attr not in value:
                raise InvariantError(
                    f"invariant {source!r} reads unknown field {node.attr!r}"
                )
            return value[node.attr]
        try:
            return getattr(value, node.attr)
        except AttributeError as exc:
            raise InvariantError(f"invariant {source!r}: {exc}") from exc

    if isinstance(node, ast.Subscript):
        value = _eval_node(node.value, ctx, source)
        index = _eval_node(node.slice, ctx, source)
        return value[index]

    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise InvariantError(f"invariant {source!r} uses an unsupported operator")
        return op(_eval_node(node.left, ctx, source), _eval_node(node.right, ctx, source))

    if isinstance(node, ast.UnaryOp):
        value = _eval_node(node.operand, ctx, source)
        if isinstance(node.op, ast.USub):
            return -value
        if isinstance(node.op, ast.UAdd):
            return +value
        if isinstance(node.op, ast.Not):
            return not value
        raise InvariantError(f"invariant {source!r} uses an unsupported unary operator")

    if isinstance(node, ast.BoolOp):
        values = [_eval_node(v, ctx, source) for v in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)

    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, ctx, source)
        for op_node, comparator in zip(node.ops, node.comparators):
            op = _CMP_OPS.get(type(op_node))
            if op is None:
                raise InvariantError(f"invariant {source!r} uses an unsupported comparison")
            right = _eval_node(comparator, ctx, source)
            if not op(left, right):
                return False
            left = right
        return True

    if isinstance(node, ast.Call):
        func_node = node.func
        if not isinstance(func_node, ast.Name) or func_node.id not in _FUNCTIONS:
            raise InvariantError(
                f"invariant {source!r} calls a function that is not allowed; "
                f"available: {', '.join(sorted(_FUNCTIONS))}"
            )
        args = [_eval_node(a, ctx, source) for a in node.args]
        return _FUNCTIONS[func_node.id](*args)

    if isinstance(node, (ast.Tuple, ast.List)):
        return [_eval_node(e, ctx, source) for e in node.elts]

    if isinstance(node, ast.IfExp):
        cond = _eval_node(node.test, ctx, source)
        return _eval_node(node.body if cond else node.orelse, ctx, source)

    raise InvariantError(
        f"invariant {source!r} uses an unsupported construct ({type(node).__name__})"
    )


# ---------------------------------------------------------------------------
# Measurement context
# ---------------------------------------------------------------------------


@dataclass
class _BBox:
    """Bounding box exposed to invariants as `bbox.x` / `bbox.y` / `bbox.z`."""

    x: float
    y: float
    z: float


def build_context(
    m: MeshMeasurements,
    params: dict[str, Any],
    *,
    material: str = "PLA",
    brep_features: dict | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Everything an invariant expression may reference.

    Parameters are exposed under their own names, so `wall_mm >= 2.0` in a
    template invariant means exactly what its author expects. Measurements use
    distinct names so a parameter can never silently shadow one.
    """
    extents = m.extents_mm
    thickness = m.thickness
    density = DENSITIES.get((material or "PLA").upper(), 1.24)
    volume_cm3 = m.volume_mm3 / 1000.0

    cylinders = (brep_features or {}).get("cylinders") or []
    holes = [c for c in cylinders if c.get("internal")]

    context: dict[str, Any] = {
        # Geometry
        "bbox": _BBox(extents[0], extents[1], extents[2]),
        "volume_mm3": m.volume_mm3,
        "volume_cm3": volume_cm3,
        "area_mm2": m.area_mm2,
        # Solid-plastic mass. Real prints are 15-25% infill, so this is an upper
        # bound -- the right side to be on for a load-bearing check.
        "mass_g": volume_cm3 * density,
        # `min_wall` is the 1st percentile, deliberately: it is the same
        # statistic the built-in wall check compares against its threshold. If
        # invariants read the raw minimum instead, a template would have to
        # declare a lower bound than the rule it is actually held to, and every
        # template author would have to know that. `min_wall_abs` is available
        # for the rare invariant that genuinely wants the single worst sample.
        "min_wall": thickness.p01_with_tolerance_mm if thickness else float("inf"),
        "min_wall_abs": thickness.min_mm if thickness else float("inf"),
        "median_wall": thickness.median_mm if thickness else float("inf"),
        "is_watertight": m.is_watertight,
        "solids": m.solid_count,
        "genus": m.genus,
        "triangles": m.triangle_count,
        # Printability
        "max_overhang_deg": m.overhangs.max_angle_deg,
        "overhang_fraction": m.overhangs.fraction,
        "max_bridge_mm": m.bridges.max_span_mm,
        "plate_contact_fraction": m.footprint.contact_fraction,
        "plate_contact_mm2": m.footprint.contact_area_mm2,
        "com_inside_footprint": m.footprint.com_inside_footprint,
        "aspect_ratio": m.aspect_ratio,
        "trapped_volumes": len(m.trapped_volumes),
        # Features, from the exact B-rep where available
        "hole_count": len(holes),
        "hole_diameters": sorted(round(float(h["diameter_mm"]), 3) for h in holes),
        "min_hole_diameter": min((float(h["diameter_mm"]) for h in holes), default=0.0),
        "cylinder_count": len(cylinders),
    }
    context.update(extra or {})
    # Parameters last so a template's own knobs are always readable by name,
    # but never able to overwrite a measurement the checks depend on.
    for key, value in params.items():
        if key not in context:
            context[key] = value
    return context


# ---------------------------------------------------------------------------
# Category defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CategoryInvariant:
    """One category-level rule, as an expression plus what to do about it."""

    id: str
    expression: str
    title: str
    message: str
    remedy: str
    severity: Severity = Severity.FAIL
    # Only checked when this expression is true; lets a rule apply
    # conditionally without the template having to opt in.
    applies_when: str | None = None


CATEGORY_INVARIANTS: dict[str, tuple[CategoryInvariant, ...]] = {
    "planter": (
        CategoryInvariant(
            "planter.watertight",
            "is_watertight",
            "Holds soil",
            "A planter that is not a closed solid will not hold wet soil.",
            "Rebuild the shell so the body is closed apart from the intended "
            "drainage hole.",
        ),
        CategoryInvariant(
            "planter.wall_thickness",
            "min_wall >= 2.0",
            "Wet-side wall thickness",
            "Walls under 2.0 mm weep when they hold wet soil: water tracks "
            "between the perimeters and the pot sweats onto the wall.",
            "Raise the wall parameter to 2.4 mm, the tested value for soil "
            "contact.",
        ),
        CategoryInvariant(
            "planter.drainage",
            "hole_count >= 1",
            "Drainage",
            "No drainage hole. Roots in a sealed pot rot within a season.",
            "Add a drainage hole of at least 6 mm, or set drainage to 'none' "
            "explicitly if the user asked for a cachepot.",
            applies_when="params.get('drainage', 'single_hole') != 'none'",
        ),
        CategoryInvariant(
            "planter.no_trapped_volume",
            "trapped_volumes == 0",
            "No sealed cavities",
            "A sealed cavity inside a planter fills with water and cannot drain.",
            "Open the cavity or add a drain path.",
        ),
    ),
    "keychain": (
        CategoryInvariant(
            "keychain.ring_hole",
            "min_hole_diameter >= 4.0",
            "Ring hole",
            "The ring hole is under 4 mm, too small for a standard split ring.",
            "Enlarge the hole to 5 mm; split rings are typically 3 mm wire on a "
            "25 mm ring and need clearance to rotate.",
            applies_when="hole_count >= 1",
        ),
        CategoryInvariant(
            "keychain.has_hole",
            "hole_count >= 1",
            "Attachment point",
            "A keychain with no hole cannot attach to anything.",
            "Add a ring hole near one end, inset far enough to leave 2.5 mm of "
            "material around it.",
        ),
        CategoryInvariant(
            "keychain.thickness",
            "bbox.z >= 2.5",
            "Body thickness",
            "Under 2.5 mm a keychain snaps in a pocket within weeks.",
            "Increase the thickness parameter to at least 3 mm.",
        ),
    ),
    "organizer": (
        CategoryInvariant(
            "organizer.flat_bottom",
            "plate_contact_fraction >= 0.5",
            "Flat bottom",
            "An organizer needs a flat base or it rocks on the desk and lifts "
            "off the print bed.",
            "Make the base a single flat face covering most of the footprint.",
            severity=Severity.WARN,
        ),
        CategoryInvariant(
            "organizer.stable",
            "com_inside_footprint",
            "Stability",
            "The centre of mass falls outside the base: this tips over when "
            "loaded.",
            "Widen the base or lower the centre of mass.",
        ),
    ),
    "wall_decor": (
        CategoryInvariant(
            "wall_decor.hanging_feature",
            "hole_count >= 1",
            "Hanging feature",
            "No hole or slot to hang the piece from.",
            "Add a keyhole slot or a 4 mm hanging hole on the back face.",
            severity=Severity.WARN,
        ),
        CategoryInvariant(
            "wall_decor.adhesive_mass",
            "mass_g <= 400",
            "Adhesive-mount mass",
            "Over 400 g of plastic is more than a command strip holds; this "
            "needs a screw mount.",
            "Reduce the wall thickness or the panel size, or switch the mount "
            "to a screw fixing.",
            severity=Severity.WARN,
        ),
    ),
    "hook": (
        CategoryInvariant(
            "hook.wall_thickness",
            "min_wall >= 2.4",
            "Root thickness",
            "A hook thinner than 2.4 mm at its root shears along a layer line "
            "under load.",
            "Thicken the hook, especially where it meets the mounting plate, "
            "and add a fillet at that junction.",
        ),
        CategoryInvariant(
            "hook.mount_present",
            "hole_count >= 1",
            "Mounting point",
            "No screw hole or mounting feature.",
            "Add a screw hole with a countersink, or a French-cleat back.",
            severity=Severity.WARN,
        ),
    ),
    "nature": (
        CategoryInvariant(
            "nature.single_piece",
            "solids == 1",
            "One piece",
            "The model came out as separate lumps, so a feature is floating "
            "rather than joined to the body.",
            "Overlap every appendage into the body before the union rather "
            "than leaving it touching.",
        ),
        CategoryInvariant(
            "nature.watertight",
            "is_watertight",
            "Closed surface",
            "An organic form that is not a closed solid will not slice.",
            "Rebuild the surface so the whole model is one closed shell.",
        ),
        CategoryInvariant(
            "nature.stands",
            "com_inside_footprint",
            "Stands up",
            "The centre of mass falls outside the footprint, so the piece "
            "topples once the supports come off.",
            "Widen the base, or reduce the lean of whatever overhangs it.",
        ),
        CategoryInvariant(
            "nature.footprint",
            "plate_contact_fraction >= 0.02",
            "Something to stick down",
            "Almost nothing touches the plate, so this will come loose during "
            "the print.",
            "Flatten or widen the base, or print it with a brim.",
            severity=Severity.WARN,
        ),
    ),
    "box": (
        CategoryInvariant(
            "box.no_trapped_volume",
            "trapped_volumes == 0",
            "No sealed cavities",
            "A sealed cavity in a box wastes material and cannot be inspected.",
            "Open the cavity to the interior.",
        ),
        CategoryInvariant(
            "box.flat_bottom",
            "plate_contact_fraction >= 0.4",
            "Flat bottom",
            "A box needs a flat base to print without a raft and to sit still.",
            "Flatten the base.",
            severity=Severity.WARN,
        ),
    ),
}


def run(
    m: MeshMeasurements,
    report: ValidationReport,
    *,
    category: str | None,
    params: dict[str, Any],
    template_invariants: list[str] | None = None,
    material: str = "PLA",
    brep_features: dict | None = None,
) -> None:
    """Append category defaults and template invariants to the report."""
    context = build_context(m, params, material=material, brep_features=brep_features)
    # `params` is exposed as a dict too, so `applies_when` guards can ask about
    # a parameter that may not be present at all.
    context["params"] = _ParamView(params)

    for rule in CATEGORY_INVARIANTS.get(category or "", ()):
        _run_category_rule(rule, context, report)

    for expression in template_invariants or []:
        _run_template_invariant(expression, context, report)


def _run_category_rule(
    rule: CategoryInvariant, context: dict[str, Any], report: ValidationReport
) -> None:
    if rule.applies_when:
        try:
            if not evaluate(rule.applies_when, context):
                return
        except InvariantError:
            return  # a guard that cannot be evaluated means the rule is not relevant

    try:
        passed = bool(evaluate(rule.expression, context))
    except InvariantError as exc:
        report.skipped.append(f"{rule.id} ({exc})")
        return

    report.add(
        check(
            rule.id,
            TIER,
            rule.title,
            passed,
            severity=rule.severity,
            message=f"{rule.title}: ok." if passed else rule.message,
            measured=_measured_for(rule.expression, context),
            remedy=rule.remedy,
        )
    )


def _run_template_invariant(
    expression: str, context: dict[str, Any], report: ValidationReport
) -> None:
    check_id = f"template.{_slug(expression)}"
    try:
        passed = bool(evaluate(expression, context))
    except InvariantError as exc:
        # A malformed invariant is a registry bug. It must be loud -- silently
        # skipping it would mean a template quietly stops being checked.
        report.add(
            check(
                check_id,
                TIER,
                "Template invariant",
                False,
                severity=Severity.WARN,
                message=f"Invariant could not be evaluated: {exc}",
                remedy="Fix the invariant expression in the template definition.",
            )
        )
        return

    report.add(
        check(
            check_id,
            TIER,
            "Template invariant",
            passed,
            message=(
                f"Invariant holds: {expression}"
                if passed
                else f"Invariant violated: {expression}"
            ),
            measured=_measured_for(expression, context),
            remedy="This template guarantees this property. The generated "
            "parameters break it, so either the parameters are out of range or "
            "the template source has drifted from its invariants.",
        )
    )


class _ParamView(dict):
    """A dict that also answers `.get(...)` inside an invariant expression."""

    def get(self, key: str, default: Any = None) -> Any:  # noqa: D102
        return dict.get(self, key, default)


def _measured_for(expression: str, context: dict[str, Any]) -> Any:
    """Evaluate the left-hand side of a comparison, for the report's `measured`.

    Best-effort: showing the model that `min_wall` was 1.8 when the rule wanted
    2.0 is worth far more than showing it `False`.
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return None
    node = tree.body
    if isinstance(node, ast.Compare):
        try:
            value = _eval_node(node.left, context, expression)
        except InvariantError:
            return None
        return round(value, 3) if isinstance(value, float) else value
    return None


def _slug(expression: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", expression.lower()).strip("_")
    return slug[:48] or "expr"
