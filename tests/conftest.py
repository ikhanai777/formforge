"""Shared fixtures.

The expensive objects -- the registry, the sandbox, a built model -- are
session-scoped. Building a solid runs the OCCT kernel and takes seconds, so a
per-test fixture would make the suite slow enough that people stop running it.
"""

from __future__ import annotations

import os
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


# --------------------------------------------------------------------------
# Accounts: the same suite, both backends
# --------------------------------------------------------------------------
# The requirement is that Postgres preserve the SQLite ledger semantics
# exactly. The only way to know that is to run one suite against both, so
# `accounts` is parametrised rather than duplicated: a test written once
# proves the property twice, and a semantic that drifts fails immediately
# instead of at the first production charge.
#
# Postgres is skipped, not failed, when no server is configured -- a
# contributor without one still gets the SQLite half. CI sets the variable and
# gets both; `docs/phase-1-plan.md` says how to start one locally.
PG_DSN_ENV = "FORMFORGE_TEST_PG"


def _postgres_dsn() -> str | None:
    return os.environ.get(PG_DSN_ENV) or None


@pytest.fixture(params=["sqlite", "postgres"])
def accounts_backend(request) -> str:
    if request.param == "postgres" and not _postgres_dsn():
        pytest.skip(f"no PostgreSQL configured; set {PG_DSN_ENV} to run these")
    return request.param


@pytest.fixture
def accounts_target(accounts_backend, tmp_path) -> str:
    """Where the store under test lives -- a path or a DSN.

    Exposed separately from `accounts` because the concurrency tests need to
    open *more* stores against the same database, which is the only way to get
    genuinely separate connections racing each other.

    Postgres gets a fresh schema per test rather than a fresh database:
    dropping and recreating a database costs a connection teardown and makes
    the suite crawl, while `DROP SCHEMA public CASCADE` is instant and just as
    total.
    """
    if accounts_backend == "sqlite":
        return str(tmp_path / "accounts.db")
    import psycopg

    dsn = _postgres_dsn()
    with psycopg.connect(dsn, autocommit=True) as setup:
        setup.execute("DROP SCHEMA IF EXISTS public CASCADE")
        setup.execute("CREATE SCHEMA public")
    return dsn


@pytest.fixture
def accounts(accounts_target):
    """A clean AccountStore on whichever backend is under test."""
    from formforge.accounts import AccountStore

    store = AccountStore(accounts_target)
    try:
        yield store
    finally:
        store.close()
