"""Shared fixtures.

The expensive objects -- the registry, the sandbox, a built model -- are
session-scoped. Building a solid runs the OCCT kernel and takes seconds, so a
per-test fixture would make the suite slow enough that people stop running it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from formforge.registry import TemplateRegistry
from formforge.sandbox import ExecuteRequest, GeometrySandbox


@pytest.fixture(scope="session")
def registry() -> TemplateRegistry:
    return TemplateRegistry.load()


@pytest.fixture(scope="session")
def sandbox() -> GeometrySandbox:
    return GeometrySandbox(keep_workdir=True)


@pytest.fixture(scope="session")
def built_tag(registry, sandbox):
    """A real keychain tag, built once by the CAD kernel and shared."""
    template = registry.get("keychain_text_tag")
    params = template.merge_params({"text": "TEST"})
    result = sandbox.execute(
        ExecuteRequest(
            source=template.render_source(params),
            language=template.language,
            params=params,
            enforce_named_constants=False,
        )
    )
    assert result.ok, result.feedback()
    return result


@pytest.fixture
def scratch(tmp_path) -> Path:
    return tmp_path
