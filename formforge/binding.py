"""Bind parameter values into a script by rewriting its constant declarations.

The alternative -- injecting parameters as globals before `exec` -- produces a
script that only runs inside our executor. Rewriting the module-level constants
instead means the `source.py` we ship in the bundle is a standalone file the
user can run, edit and re-run in their own checkout of build123d. That is the
whole argument for the parametric approach (spec section 3.2), so it is worth
the extra machinery.

The mapping between a schema's `width_mm` and a script's `WIDTH_MM` is
upper-casing, and nothing cleverer: a rule you can explain in one sentence is a
rule the model gets right.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

# Values a constant may hold. Anything else (a list of points, a computed
# expression) is not a parameter and is left alone.
Bindable = float | int | str | bool | None


@dataclass
class BindResult:
    """The rewritten source plus a record of what actually changed."""

    source: str
    bound: dict[str, Bindable] = field(default_factory=dict)
    unbound: dict[str, Bindable] = field(default_factory=dict)
    declared: dict[str, Bindable] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.unbound


class BindingError(Exception):
    """Raised when a required parameter has no matching constant in the script."""


def constant_name(param: str) -> str:
    """The module-level constant name a schema parameter maps to."""
    return param.upper()


def declared_constants(source: str) -> dict[str, Bindable]:
    """Every module-level UPPER_CASE constant with a literal value."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    found: dict[str, Bindable] = {}
    for stmt in tree.body:
        for name, value in _constant_assignments(stmt):
            found[name] = value
    return found


def _constant_assignments(stmt: ast.stmt) -> list[tuple[str, Bindable]]:
    targets: list[ast.expr]
    value: ast.expr
    if isinstance(stmt, ast.Assign):
        targets, value = list(stmt.targets), stmt.value
    elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        targets, value = [stmt.target], stmt.value
    else:
        return []
    out: list[tuple[str, Bindable]] = []
    for target in targets:
        if not isinstance(target, ast.Name) or not _is_constant_name(target.id):
            continue
        literal = _literal(value)
        if literal is not _SENTINEL:
            out.append((target.id, literal))  # type: ignore[arg-type]
    return out


def _is_constant_name(name: str) -> bool:
    """UPPER_SNAKE_CASE, allowing digits: WALL_MM, HOLE_D2, TEXT."""
    return name.isupper() and not name.startswith("_")


class _Sentinel:
    pass


_SENTINEL = _Sentinel()


def _literal(node: ast.expr) -> Bindable | _Sentinel:
    try:
        value = ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return _SENTINEL
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return _SENTINEL


class _Binder(ast.NodeTransformer):
    def __init__(self, values: dict[str, Bindable]):
        self.values = values
        self.applied: dict[str, Bindable] = {}
        self._depth = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        return self._nested(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        return self._nested(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        return self._nested(node)

    def _nested(self, node: ast.AST) -> ast.AST:
        # Only module-level constants are parameters. A same-named local inside
        # a function is that function's business.
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1
        return node

    def visit_Assign(self, node: ast.Assign) -> ast.AST:
        if self._depth == 0:
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in self.values:
                    node.value = _to_node(self.values[target.id])
                    self.applied[target.id] = self.values[target.id]
        return node

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST:
        if (
            self._depth == 0
            and node.value is not None
            and isinstance(node.target, ast.Name)
            and node.target.id in self.values
        ):
            node.value = _to_node(self.values[node.target.id])
            self.applied[node.target.id] = self.values[node.target.id]
        return node


def _to_node(value: Bindable) -> ast.expr:
    return ast.Constant(value=value)


def bind(
    source: str,
    params: dict[str, object],
    *,
    strict: bool = False,
) -> BindResult:
    """Rewrite module-level constants in `source` from `params`.

    A parameter with no matching constant is reported in `unbound` rather than
    silently dropped -- a schema and a script that have drifted apart produce a
    model with the wrong dimensions and no error, which is the worst possible
    outcome. With `strict=True` the drift raises instead.
    """
    declared = declared_constants(source)
    values: dict[str, Bindable] = {}
    unbound: dict[str, Bindable] = {}
    for key, raw in params.items():
        name = constant_name(key)
        if not isinstance(raw, (int, float, str, bool)) and raw is not None:
            # Structured parameters (lists, nested objects) are passed through
            # to the script's own logic, not bound as constants.
            unbound[key] = None
            continue
        if name in declared:
            values[name] = raw  # type: ignore[assignment]
        else:
            unbound[key] = raw  # type: ignore[assignment]

    if strict and unbound:
        missing = ", ".join(f"{k} (expected constant {constant_name(k)})" for k in sorted(unbound))
        raise BindingError(f"script declares no constant for: {missing}")

    if not values:
        return BindResult(source=source, bound={}, unbound=unbound, declared=declared)

    tree = ast.parse(source)
    binder = _Binder(values)
    binder.visit(tree)
    ast.fix_missing_locations(tree)
    return BindResult(
        source=ast.unparse(tree),
        bound=binder.applied,
        unbound=unbound,
        declared=declared,
    )


def bound_source(source: str, params: dict[str, object]) -> str:
    """Convenience wrapper returning just the rewritten source."""
    return bind(source, params).source


def extract_params(source: str, schema: dict[str, object] | None = None) -> dict[str, object]:
    """Read current parameter values back out of a script.

    Used to populate `params.json` for a freeform generation, where there is no
    template schema to read defaults from. When a schema is supplied, only keys
    it declares are returned, so an internal constant does not become a UI
    slider.
    """
    declared = declared_constants(source)
    if schema is None:
        return {name.lower(): value for name, value in declared.items()}
    props = schema.get("properties")
    keys = set(props) if isinstance(props, dict) else set()
    return {
        name.lower(): value
        for name, value in declared.items()
        if name.lower() in keys
    }
