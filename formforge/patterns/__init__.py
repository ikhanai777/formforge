"""Printable pattern panels: a continuous relief surface, cut into tiles that join.

The public surface is small on purpose. A caller picks a pattern from
`PATTERNS`, describes the panel with a `PanelSpec`, and gets a directory of
printable tiles plus the map that explains them:

    from formforge.patterns import PanelSpec, build_panel

    result = build_panel(
        PanelSpec(pattern_id="dunes", width_mm=600, height_mm=400, seed=3),
        "out/dunes",
    )

Why this lives beside the template registry rather than inside it: a template is
a parametric CAD script with a dozen dimensions, and a pattern panel is a
sampled surface with a quarter of a million of them. They share the printer
profiles, the DFM thresholds and the validation engine, and they share nothing
else -- so they share those and stay apart.
"""

from __future__ import annotations

from .field import Bounds, PatternField
from .joinery import JOINT_STYLES, MOUNT_STYLES, JointSpec, MountSpec
from .library import FAMILIES, PATTERNS, Param, PatternSpec, get_pattern, resolve_params
from .panel import PanelError, PanelResult, PanelSpec, TileOutput, build_panel
from .surface import SurfaceError
from .tiling import TilePlan, TileSpec, plan_panel

__all__ = [
    "FAMILIES",
    "JOINT_STYLES",
    "MOUNT_STYLES",
    "PATTERNS",
    "Bounds",
    "JointSpec",
    "MountSpec",
    "PanelError",
    "PanelResult",
    "PanelSpec",
    "Param",
    "PatternField",
    "PatternSpec",
    "SurfaceError",
    "TileOutput",
    "TilePlan",
    "TileSpec",
    "build_panel",
    "get_pattern",
    "plan_panel",
    "resolve_params",
]
