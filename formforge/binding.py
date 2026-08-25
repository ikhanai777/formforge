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


@dataclass
class _Edit:
    """One constant's value, located precisely in the source text."""

    name: str
    value: Bindable
    lineno: int
    end_lineno: int
    col_offset: int
    end_col_offset: int


def _find_edits(tree: ast.Module, values: dict[str, Bindable]) -> list[_Edit]:
    """Locate the module-level constant assignments to rewrite.

    Only module-level statements are considered: a same-named local inside a
    function is that function's business, not a parameter.
    """
    edits: list[_Edit] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            targets, value = list(stmt.targets), stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in values:
                edits.append(
                    _Edit(
                        name=target.id,
                        value=values[target.id],
                        lineno=value.lineno,
                        end_lineno=value.end_lineno or value.lineno,
                        col_offset=value.col_offset,
                        end_col_offset=value.end_col_offset or value.col_offset,
                    )
                )
    return edits


def _apply_edits(source: str, edits: list[_Edit]) -> str:
    """Rewrite the located values in place, leaving the rest of the file alone.

    Textual rather than an AST round-trip. `ast.unparse` would produce a correct
    script but throw away every comment, blank line and formatting choice --
    and the comments in a template are where the reasoning lives ("chamfer
    before cutting the hole, or the hole's rim gets chamfered too"). Since the
    bundle's `source.py` is meant to be read and edited by the user, losing them
    would gut the artifact this whole approach exists to produce.
    """
    lines = source.splitlines(keepends=True)
    # Apply from the end so earlier edits do not shift later offsets.
    for edit in sorted(edits, key=lambda e: (e.lineno, e.col_offset), reverse=True):
        start_index = edit.lineno - 1
        end_index = edit.end_lineno - 1
        if start_index < 0 or end_index >= len(lines):
            continue
        replacement = repr(edit.value)
        if start_index == end_index:
            line = lines[start_index]
            lines[start_index] = (
                line[: edit.col_offset] + replacement + line[edit.end_col_offset :]
            )
        else:
            head = lines[start_index][: edit.col_offset]
            tail = lines[end_index][edit.end_col_offset :]
            lines[start_index : end_index + 1] = [head + replacement + tail]
    return "".join(lines)


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
    edits = _find_edits(tree, values)
    rewritten = _apply_edits(source, edits)

    # A rewrite that does not parse would be shipped to the user as a broken
    # script and only fail when they ran it. Cheaper to catch here.
    try:
        ast.parse(rewritten)
    except SyntaxError as exc:
        raise BindingError(
            f"binding parameters produced a script that does not parse: {exc.msg} "
            f"at line {exc.lineno}"
        ) from exc

    return BindResult(
        source=rewritten,
        bound={edit.name: edit.value for edit in edits},
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
