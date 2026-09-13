"""HTTP surface. Import ``create_app`` lazily -- it needs the ``api`` extra."""

from __future__ import annotations

from .app import FASTAPI_AVAILABLE, create_app

__all__ = ["FASTAPI_AVAILABLE", "create_app"]
