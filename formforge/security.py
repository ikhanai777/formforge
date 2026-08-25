"""Static AST gate for LLM-authored geometry scripts (spec section 10.2).

This is defence in depth, not the primary control. The sandbox (no network,
gVisor/rlimits, ephemeral container) is the primary control. Assume this gate is
bypassable and that it will be bypassed; its job is to turn the common,
uninteresting cases -- a model that reaches for `os` out of habit, a prompt
injection that asks for `open('/etc/passwd')` -- into a cheap, legible rejection
before a container is ever spawned.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

# Modules a geometry script may import. Anything that can touch the filesystem,
# the network, another process, or the interpreter's own machinery is absent by
# design.
ALLOWED_IMPORTS: frozenset[str] = frozenset(
    {
        "build123d",
        "cadquery",
        "math",
        "cmath",
        "numpy",
        "trimesh",
        "typing",
        "dataclasses",
        "enum",
        "itertools",
        "functools",
        "statistics",
        "random",
        "copy",
    }
)

# Names that are never legitimate in a geometry script.
BANNED_NAMES: frozenset[str] = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "__import__",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "breakpoint",
        "memoryview",
        "exit",
        "quit",
    }
)

# Constructors whose numeric arguments must be named constants rather than
# magic numbers (spec section 6.3). This is a style rule with a real payoff: it
# is what makes a generated script editable and the UI sliders possible.
GEOMETRY_CALLS: frozenset[str] = frozenset(
    {
        "Box",
        "Cylinder",
        "Sphere",
        "Cone",
        "Torus",
        "Wedge",
        "Rectangle",
        "RectangleRounded",
        "Circle",
        "Ellipse",
        "Polygon",
        "RegularPolygon",
        "Triangle",
        "SlotOverall",
        "SlotCenterToCenter",
        "Text",
        "extrude",
        "revolve",
        "loft",
        "sweep",
        "offset",
        "fillet",
        "chamfer",
        "Hole",
        "CounterBoreHole",
        "CounterSinkHole",
        "Pos",
        "Rot",
        "Location",
        "Plane",
        "Vector",
        "Line",
        "Polyline",
        "Spline",
        "RadiusArc",
        "SagittaArc",
        "CenterArc",
        "TangentArc",
        "JernArc",
        "EllipticalCenterArc",
        "mirror",
        "scale",
        "split",
        "make_face",
        "thicken",
        "shell",
        "sweep_",
    }
)

# Arguments that are genuinely fine as literals: counts, flags, mode selectors,
# and the small integers that index an axis. Requiring a named constant for
# `align=(Align.CENTER,)` or `rotation=0` produces noise, not editability.
LITERAL_OK_KEYWORDS: frozenset[str] = frozenset(
    {
        "count",
        "n",
        "sides",
        "segments",
        "closed",
        "clean",
        "mode",
        "align",
        "kind",
        "side",
        "keep",
        "both",
        "taper",
        "font",
        "font_style",
        "font_path",
        "sort_by",
        "axis",
        "dir",
        "until",
        "target",
        "transition",
        "is_frenet",
        "normal",
        "arc_size",
        "start_angle",
        "rotation",
        "angle",
    }
)

# Small literals that are structural rather than dimensional: 0 and 1 turn up as
# indices, identity scales and axis selectors constantly.
LITERAL_OK_VALUES: frozenset[float] = frozenset({0.0, 1.0, -1.0, 2.0, 360.0, 180.0, 90.0})

# Positional arguments that are counts rather than dimensions, by constructor.
# `RegularPolygon(radius, side_count)` -- the radius is a dimension and belongs
# in a constant, the side count is what makes it a hexagon. Requiring
# `SIDES = 6` adds a knob that means nothing to a user and would be wrong to
# expose as a slider. Listed explicitly per constructor rather than inferred
# from the value, because `Box(60, 40, 3)` is three dimensions that happen to
# be small integers.
POSITIONAL_COUNT_ARGS: dict[str, frozenset[int]] = {
    "RegularPolygon": frozenset({1}),
    "PolarLocations": frozenset({1}),
    "GridLocations": frozenset({2, 3}),
    "HexLocations": frozenset({1, 2}),
}


@dataclass
class Violation:
    """One rejected construct, with enough location to point the model at it."""

    rule: str
    message: str
    lineno: int
    col: int = 0
    severity: str = "error"  # "error" blocks execution, "warning" is advisory

    def as_dict(self) -> dict[str, object]:
        return {
            "rule": self.rule,
            "message": self.message,
            "line": self.lineno,
            "col": self.col,
            "severity": self.severity,
        }

    def __str__(self) -> str:
        return f"line {self.lineno}: [{self.rule}] {self.message}"


@dataclass
class ScanResult:
    """Outcome of a static scan: what was rejected, and what the script declares."""

    violations: list[Violation] = field(default_factory=list)
    constants: dict[str, float] = field(default_factory=dict)
    imports: set[str] = field(default_factory=set)

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "error"]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "violations": [v.as_dict() for v in self.violations],
            "constants": self.constants,
            "imports": sorted(self.imports),
        }

    def report(self) -> str:
        if self.ok:
            return "static gate: pass"
        lines = ["static gate: rejected"]
        lines += [f"  {v}" for v in self.errors]
        return "\n".join(lines)


class SecurityError(Exception):
    """Raised when a script fails the static gate and execution must not proceed."""

    def __init__(self, result: ScanResult):
        self.result = result
        super().__init__(result.report())


class _Scanner(ast.NodeVisitor):
    def __init__(self, enforce_named_constants: bool):
        self.result = ScanResult()
        self.enforce_named_constants = enforce_named_constants
        # Depth of geometry-call nesting, so `Box(WIDTH, Cylinder(D).x, H)`
        # still checks the inner call's arguments.
        self._geometry_depth = 0
        self._assign_depth = 0

    # -- reporting ---------------------------------------------------------
    def _reject(self, node: ast.AST, rule: str, message: str) -> None:
        self.result.violations.append(
            Violation(rule, message, getattr(node, "lineno", 0), getattr(node, "col_offset", 0))
        )

    def _warn(self, node: ast.AST, rule: str, message: str) -> None:
        self.result.violations.append(
            Violation(
                rule,
                message,
                getattr(node, "lineno", 0),
                getattr(node, "col_offset", 0),
                severity="warning",
            )
        )

    # -- imports -----------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            self.result.imports.add(root)
            if root not in ALLOWED_IMPORTS:
                self._reject(
                    node,
                    "import",
                    f"import of {alias.name!r} is not allowed; "
                    f"permitted modules: {', '.join(sorted(ALLOWED_IMPORTS))}",
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        root = (node.module or "").split(".")[0]
        self.result.imports.add(root)
        if node.level:
            self._reject(node, "import", "relative imports are not allowed")
        elif root not in ALLOWED_IMPORTS:
            self._reject(
                node,
                "import",
                f"import from {node.module!r} is not allowed; "
                f"permitted modules: {', '.join(sorted(ALLOWED_IMPORTS))}",
            )
        self.generic_visit(node)

    # -- names and attributes ---------------------------------------------
    def visit_Name(self, node: ast.Name) -> None:
        if node.id in BANNED_NAMES:
            self._reject(node, "banned-name", f"use of {node.id!r} is not allowed")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        name = node.attr
        if name.startswith("__") and name.endswith("__"):
            self._reject(
                node,
                "dunder-attribute",
                f"access to dunder attribute {name!r} is not allowed",
            )
        self.generic_visit(node)

    # -- loops -------------------------------------------------------------
    def visit_While(self, node: ast.While) -> None:
        if not _provably_bounded(node):
            self._reject(
                node,
                "unbounded-while",
                "while loop has no provable bound; rewrite as a for loop over a "
                "range, or add an explicit counter with a constant limit",
            )
        self.generic_visit(node)

    # -- magic numbers in geometry calls -----------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        func_name = _call_name(node.func)
        is_geometry = func_name in GEOMETRY_CALLS
        if is_geometry and self.enforce_named_constants:
            self._check_literals(node, func_name)
        if is_geometry:
            self._geometry_depth += 1
        self.generic_visit(node)
        if is_geometry:
            self._geometry_depth -= 1

    def _check_literals(self, node: ast.Call, func_name: str) -> None:
        count_positions = POSITIONAL_COUNT_ARGS.get(func_name, frozenset())
        for index, arg in enumerate(node.args):
            if index in count_positions:
                continue
            self._check_literal_arg(arg, func_name, None)
        for kw in node.keywords:
            if kw.arg in LITERAL_OK_KEYWORDS:
                continue
            self._check_literal_arg(kw.value, func_name, kw.arg)

    def _check_literal_arg(self, arg: ast.AST, func_name: str, kwname: str | None) -> None:
        for value, where in _numeric_literals(arg):
            if value in LITERAL_OK_VALUES:
                continue
            label = f"{func_name}({kwname}=...)" if kwname else f"{func_name}(...)"
            self._reject(
                where,
                "magic-number",
                f"numeric literal {value:g} passed to {label}; declare it as a "
                f"named module-level constant so it can be exposed as a parameter",
            )

    # -- module-level constants -------------------------------------------
    def visit_Module(self, node: ast.Module) -> None:
        for stmt in node.body:
            self._collect_constant(stmt)
        self.generic_visit(node)

    def _collect_constant(self, stmt: ast.stmt) -> None:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign):
            targets = list(stmt.targets)
            value = stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets = [stmt.target]
            value = stmt.value
        if value is None:
            return
        for target in targets:
            if isinstance(target, ast.Name) and target.id.isupper():
                literal = _const_number(value)
                if literal is not None:
                    self.result.constants[target.id] = literal


def _call_name(func: ast.expr) -> str:
    """The bare callable name, whether called directly or through a module."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _numeric_literals(node: ast.AST) -> list[tuple[float, ast.AST]]:
    """Numeric literals reachable from an argument expression.

    Descends through tuples, lists and unary minus (so `-3.5` is caught) but
    stops at any Name or Call: `WIDTH / 2` is a legitimate expression built from
    a named constant, and `Vector(X, Y, Z)` is somebody else's problem to check.
    """
    found: list[tuple[float, ast.AST]] = []

    def walk(n: ast.AST) -> None:
        if isinstance(n, ast.Constant):
            if isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
                found.append((float(n.value), n))
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            for value, where in _numeric_literals(n.operand):
                found.append((-value, where))
        elif isinstance(n, (ast.Tuple, ast.List)):
            for elt in n.elts:
                walk(elt)
        elif isinstance(n, ast.BinOp):
            walk(n.left)
            walk(n.right)

    walk(node)
    return found


def _const_number(node: ast.expr) -> float | None:
    """Evaluate a constant expression built only from literals and arithmetic."""
    try:
        value = ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _provably_bounded(node: ast.While) -> bool:
    """Heuristic: does this while loop obviously terminate?

    Accepts a loop whose test compares a counter against a constant and whose
    body increments that counter, plus the trivial `while False`. Everything
    else is rejected -- the CPU rlimit is the real backstop, this just makes the
    common `while True:` case fail fast with a legible message.
    """
    test = node.test
    if isinstance(test, ast.Constant) and not test.value:
        return True
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    if not isinstance(test.ops[0], (ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
        return False
    left = test.left
    if not isinstance(left, ast.Name):
        return False
    if _const_number(test.comparators[0]) is None and not isinstance(
        test.comparators[0], ast.Name
    ):
        return False
    counter = left.id
    for sub in ast.walk(node):
        if isinstance(sub, ast.AugAssign):
            if isinstance(sub.target, ast.Name) and sub.target.id == counter:
                return True
        if isinstance(sub, ast.Assign):
            for t in sub.targets:
                if isinstance(t, ast.Name) and t.id == counter:
                    return True
    return False


def scan(source: str, *, enforce_named_constants: bool = True) -> ScanResult:
    """Statically scan a geometry script. Never executes anything.

    `enforce_named_constants` turns the magic-number style rule on. It is on for
    freeform LLM output and off for hand-authored registry templates, which are
    human-reviewed and may legitimately use a literal in a place the heuristic
    cannot tell apart from a dimension.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        result = ScanResult()
        result.violations.append(
            Violation("syntax", f"script does not parse: {exc.msg}", exc.lineno or 0)
        )
        return result

    scanner = _Scanner(enforce_named_constants)
    scanner.visit(tree)
    return scanner.result


def enforce(source: str, *, enforce_named_constants: bool = True) -> ScanResult:
    """Scan and raise SecurityError if the script must not run."""
    result = scan(source, enforce_named_constants=enforce_named_constants)
    if not result.ok:
        raise SecurityError(result)
    return result
