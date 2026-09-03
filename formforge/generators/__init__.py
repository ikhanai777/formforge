"""Parametric generators: definitions that write parameter sets, not geometry.

A template is one model with sliders. A generator is the thing that decides
where the sliders go -- a species, a seed, and a set of relationships between
the numbers that keeps a variation looking like the same organism rather than a
different one badly drawn.

The split is deliberate and follows the same line Grasshopper draws: geometry
components on one side, sliders and expressions on the other. It also follows
the line this system already draws for a different reason -- geometry runs in
the sandbox and may not import anything from FormForge, so a generator that
wanted to reach into the geometry could not, and one that stays on this side
composes freely with the registry, the validator and the CLI.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from types import ModuleType
from typing import Any

from .graph import Component, Definition, DefinitionError, Slider, Solution

__all__ = [
    "CATALOG",
    "Component",
    "Definition",
    "DefinitionError",
    "Generator",
    "Slider",
    "Solution",
    "catalog",
]


@dataclass(frozen=True)
class Generator:
    """One definition, wired to the template it drives.

    Two generators are enough to say what they have in common: a definition, a
    template, a named variant to start from, a seed, and a solve that lands on
    a parameter set the template accepts. The variant is called a species for
    mushrooms and a style for vases -- the domain word is the one worth
    keeping, so it is data here rather than a name every caller has to learn.
    """

    name: str
    module_name: str
    variant_flag: str
    variant_noun: str
    summary: str

    @property
    def module(self) -> ModuleType:
        return import_module(self.module_name)

    @property
    def template_id(self) -> str:
        return self.module.TEMPLATE_ID

    @property
    def definition(self) -> Definition:
        return self.module.DEFINITION

    def variants(self) -> tuple[str, ...]:
        finder = getattr(self.module, f"{self.variant_flag}_names")
        return finder()

    def member_seed(self, seed: int, index: int) -> int:
        return self.module.member_seed(seed, index)

    def describe(self, params: dict[str, Any]) -> str:
        return self.module.describe(params)

    def solve(
        self,
        seed: int,
        *,
        variant: str,
        variation: float,
        overrides: dict[str, Any] | None = None,
    ) -> Solution:
        return self.module.solve(
            seed,
            variation=variation,
            overrides=overrides,
            **{self.variant_flag: variant},
        )

    def variant_of(self, solution: Solution) -> str:
        return solution["preset"][self.variant_flag]


CATALOG: tuple[Generator, ...] = (
    Generator(
        name="mushroom",
        module_name="formforge.generators.mushroom",
        variant_flag="species",
        variant_noun="species",
        summary="generate variations of a detailed mushroom and export them",
    ),
    Generator(
        name="vase",
        module_name="formforge.generators.vase",
        variant_flag="style",
        variant_noun="style",
        summary="generate variations of a printable vase and export them",
    ),
)


def catalog() -> dict[str, Generator]:
    """Every generator, by the name its command uses."""
    return {generator.name: generator for generator in CATALOG}
