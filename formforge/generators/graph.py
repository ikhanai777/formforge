"""A small dataflow graph: Grasshopper's solver, minus the canvas.

Grasshopper is not a scripting language with a picture on top. What makes a
definition behave the way it does is a handful of rules, and they are the rules
worth stealing:

* **Sliders are the only free values.** Everything else is computed. There is no
  hidden state anywhere in the definition, so a parameter set fully determines
  the model.
* **Components are pure functions of their inputs.** A component cannot see the
  world, only what is wired into it, which is why a definition can be re-solved
  from any starting point and lands in the same place.
* **The wiring is a DAG, and the solver walks it in dependency order.** Here a
  component may only consume names that already exist, so a cycle is
  unrepresentable rather than detected.
* **Recompute is deterministic and cheap.** Same inputs, same outputs; each node
  evaluates once per solve.

What that buys, for a generator: a variation is a solve, not a script run. The
definition can be printed (`explain`), the intermediate values are all
inspectable in the solution, and "why did this mushroom come out squat" is
answered by reading the node that decided it rather than by re-reading the whole
generator.

This module is deliberately about a hundred lines and knows nothing about
mushrooms; `formforge.generators.mushroom` is the definition built on it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any


class DefinitionError(Exception):
    """The graph is malformed. Always an authoring bug, never user input."""


@dataclass(frozen=True)
class Slider:
    """One free input, with the range it is allowed to take.

    `low`/`high` are the slider's own stops. They are not the authority on what
    the geometry accepts -- the template schema is -- but a definition that
    hands out values its own sliders forbid is one nobody can reason about.
    """

    name: str
    default: Any
    low: float | None = None
    high: float | None = None
    choices: tuple[Any, ...] = ()
    doc: str = ""

    def clamp(self, value: Any) -> Any:
        if self.choices:
            return value if value in self.choices else self.default
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return value
        if self.low is not None:
            value = max(self.low, value)
        if self.high is not None:
            value = min(self.high, value)
        return type(self.default)(value) if isinstance(self.default, int) else float(value)


@dataclass(frozen=True)
class Component:
    """One node: a pure function of named upstream values."""

    name: str
    inputs: tuple[str, ...]
    fn: Callable[..., Any]
    doc: str = ""


@dataclass
class Solution:
    """Every value the solve produced, keyed by node name."""

    definition: str
    values: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, name: str) -> Any:
        return self.values[name]

    def get(self, name: str, default: Any = None) -> Any:
        return self.values.get(name, default)

    def trace(self) -> str:
        """Each node and what it produced. The debugging view."""
        lines = [f"{self.definition}:"]
        for name, value in self.values.items():
            rendered = _short(value)
            lines.append(f"  {name:<14} {rendered}")
        return "\n".join(lines)


class Definition:
    """A named graph of sliders and components.

    Components are added in dependency order, which is what makes the graph
    acyclic by construction: a node cannot name an input that does not exist
    yet, and nothing can be rewired after the fact.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._sliders: dict[str, Slider] = {}
        self._components: dict[str, Component] = {}
        self._order: list[str] = []

    # -- authoring ---------------------------------------------------------
    def slider(
        self,
        name: str,
        default: Any,
        *,
        low: float | None = None,
        high: float | None = None,
        choices: Iterable[Any] = (),
        doc: str = "",
    ) -> Slider:
        if name in self._sliders or name in self._components:
            raise DefinitionError(f"{self.name}: {name!r} is already defined")
        slider = Slider(name, default, low, high, tuple(choices), doc)
        self._sliders[name] = slider
        return slider

    def component(
        self, *inputs: str, name: str | None = None
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a node. Decorated function keeps working as a function."""

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            node = name or fn.__name__
            if node in self._sliders or node in self._components:
                raise DefinitionError(f"{self.name}: {node!r} is already defined")
            for upstream in inputs:
                if upstream not in self._sliders and upstream not in self._components:
                    raise DefinitionError(
                        f"{self.name}: {node!r} consumes {upstream!r}, which is not "
                        f"defined yet. Components are wired in dependency order, so "
                        f"a cycle cannot be expressed."
                    )
            self._components[node] = Component(node, tuple(inputs), fn, fn.__doc__ or "")
            self._order.append(node)
            return fn

        return register

    # -- inspection --------------------------------------------------------
    @property
    def sliders(self) -> dict[str, Slider]:
        return dict(self._sliders)

    @property
    def components(self) -> dict[str, Component]:
        return dict(self._components)

    def order(self) -> tuple[str, ...]:
        """Evaluation order: the sliders, then the components as wired."""
        return tuple(self._sliders) + tuple(self._order)

    def defaults(self) -> dict[str, Any]:
        return {name: slider.default for name, slider in self._sliders.items()}

    def explain(self) -> str:
        """The definition as text, in the order it solves.

        The point of a node graph is that you can see it. This is the closest
        thing a terminal has to the canvas.
        """
        lines = [f"definition {self.name}", "  sliders:"]
        for slider in self._sliders.values():
            if slider.choices:
                span = "one of " + ", ".join(str(c) for c in slider.choices)
            elif slider.low is not None or slider.high is not None:
                span = f"{slider.low} .. {slider.high}"
            else:
                span = "free"
            lines.append(f"    {slider.name:<12} = {slider.default!r:<12} [{span}]")
        lines.append("  components:")
        for node in self._order:
            component = self._components[node]
            wiring = ", ".join(component.inputs) or "-"
            head = (component.doc or "").strip().splitlines()
            lines.append(f"    {node:<12} <- {wiring}")
            if head:
                lines.append(f"      {head[0]}")
        return "\n".join(lines)

    # -- solving -----------------------------------------------------------
    def solve(self, **inputs: Any) -> Solution:
        """Evaluate every node once, in dependency order."""
        unknown = set(inputs) - set(self._sliders)
        if unknown:
            raise DefinitionError(
                f"{self.name}: no slider named {', '.join(sorted(unknown))}. "
                f"Sliders are {', '.join(self._sliders)}."
            )

        values: dict[str, Any] = {}
        for name, slider in self._sliders.items():
            given = inputs.get(name, slider.default)
            values[name] = slider.clamp(given)

        for node in self._order:
            component = self._components[node]
            values[node] = component.fn(*(values[i] for i in component.inputs))

        return Solution(self.name, values, {k: values[k] for k in self._sliders})


def _short(value: Any, limit: int = 68) -> str:
    if isinstance(value, dict):
        rendered = "{" + ", ".join(f"{k}={_number(v)}" for k, v in value.items()) + "}"
    elif isinstance(value, (list, tuple)) and not isinstance(value, str):
        rendered = "[" + ", ".join(_number(v) for v in value) + "]"
    else:
        rendered = _number(value)
    return rendered if len(rendered) <= limit else rendered[: limit - 3] + "..."


def _number(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def as_sequence(value: Any) -> Sequence[Any]:
    """One value or many, always iterable. Grasshopper's list-or-item rule."""
    if isinstance(value, (list, tuple)):
        return value
    return (value,)
