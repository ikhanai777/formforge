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

from .graph import Component, Definition, DefinitionError, Slider, Solution

__all__ = ["Component", "Definition", "DefinitionError", "Slider", "Solution"]
