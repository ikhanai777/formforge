"""Image to wall-mounted key holder.

    formforge keyholder cat.png --width 200 --hooks 5

The user supplies a picture; the picture supplies a silhouette; everything that
makes the silhouette a working object -- the hook rail, self-supporting hooks,
keyhole hangers, minimum feature sizes -- is parametric geometry generated
against the same DFM rules the validator applies afterwards.

The stages, and where each one lives:

    image.py    pixels  -> a foreground mask         (which pixels are the subject)
    trace.py    mask    -> a polygon with holes      (marching squares + Shapely)
    design.py   polygon -> a checked layout in mm    (the product decisions)
    emit.py     layout  -> a standalone build123d script
    pipeline.py script  -> sandbox, validation, renders, bundle

Nothing generated from the image is trusted as geometry: the trace is cleaned,
scaled and checked in the plane before a kernel ever sees it, and the result is
then validated exactly like any other FormForge model.
"""

from __future__ import annotations

from .design import (
    DETAIL_MODES,
    MOUNT_MODES,
    PARAM_RANGES,
    DesignError,
    KeyHolderPlan,
    KeyHolderSpec,
    MountPoint,
    plan,
)
from .emit import emit
from .image import ImageError, Mask, MaskInfo, read_mask
from .pipeline import KeyHolderResult, build_key_holder, plan_from_image
from .trace import TraceError, TraceStats, trace_polygon

__all__ = [
    "DETAIL_MODES",
    "MOUNT_MODES",
    "PARAM_RANGES",
    "DesignError",
    "ImageError",
    "KeyHolderPlan",
    "KeyHolderResult",
    "KeyHolderSpec",
    "Mask",
    "MaskInfo",
    "MountPoint",
    "TraceError",
    "TraceStats",
    "build_key_holder",
    "emit",
    "plan",
    "plan_from_image",
    "read_mask",
    "trace_polygon",
]
