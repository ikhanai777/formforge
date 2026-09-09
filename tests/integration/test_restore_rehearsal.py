"""The PostgreSQL restore rehearsal. Destructive, and opt-in twice over.

A restore procedure nobody has run is a document, not a capability. This is the
rehearsal that turns it into one: write rows into a disposable database,
`pg_dump` it, **drop the schema**, `pg_restore` it, and then read the rows back
through `AccountStore` -- so the thing being proved is not "the file exists"
but "the application can serve from what came back".

It drops a schema, so the guards are deliberately awkward:

* `FORMFORGE_IT_PG_RESTORE=1` -- consent, separate from every other flag;
* `FORMFORGE_IT_DISPOSABLE_PG` -- a DSN, and the fixture refuses one whose
  database name does not contain `test` or `disposable`;
* deliberately **not** `FORMFORGE_TEST_PG`, so the variable the ordinary
  Postgres suite uses can never be the one that gets dropped.

Run it before staging, and again whenever the schema changes:

    FORMFORGE_IT_PG_RESTORE=1 \\
    FORMFORGE_IT_DISPOSABLE_PG=postgresql://localhost/formforge_disposable \\
      python -m pytest tests/integration/test_restore_rehearsal.py -v

`docs/staging-validation.md` has the procedure this rehearses.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def _drop_everything(dsn: str) -> None:
    """The disaster, on purpose."""
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


def _seed(dsn: str) -> dict:
    from formforge.accounts import AccountStore

    store = AccountStore(dsn)
    try:
        user = store.create_user("rehearsal@example.invalid", "a-long-enough-password")
        store.grant(user["id"], 40, note="rehearsal purchase")
        store.spend(user["id"], 1, model_id="rehearsal-model")
        store.record_billing_event(
            "offline", "evt_rehearsal", "purchase", user_id=user["id"]
        )
        return {"id": user["id"], "balance": store.balance(user["id"])}
    finally:
        store.close()


@pytest.fixture
def clean_disposable(disposable_postgres):
    """Start from an empty schema, and leave one behind.

    The teardown matters as much as the setup: a rehearsal that leaves rows in
    the disposable database makes the next run's assertions meaningless.
    """
    _drop_everything(disposable_postgres)
    yield disposable_postgres
    _drop_everything(disposable_postgres)


class TestTheRehearsal:
    def test_dump_drop_restore_and_the_ledger_is_still_right(
        self, clean_disposable, tmp_path
    ):
        from formforge.accounts import AccountStore
        from formforge.backup import (
            backup_postgres,
            post_restore_check,
            restore_postgres,
            verify_postgres_dump,
        )

        seeded = _seed(clean_disposable)
        before = post_restore_check(clean_disposable)
        # Non-vacuous on purpose. An earlier version of `post_restore_check`
        # wrapped the DSN in a Path, silently opened a new SQLite file, and
        # answered zero for everything -- and `after == before` held, because
        # both readings were wrong the same way.
        assert before["backend"] == "postgres"
        assert before["users"] == 1
        assert before["ledger_entries"] >= 3

        archive = backup_postgres(clean_disposable, tmp_path / "rehearsal.dump")
        assert archive.stat().st_size > 0

        # Verified before the disaster, not after: an archive checked only
        # once the original is gone is checked too late.
        tables = verify_postgres_dump(archive)
        for critical in ("users", "credit_ledger", "billing_events"):
            assert critical in tables, f"{critical} is not in the dump"

        _drop_everything(clean_disposable)

        restore_postgres(clean_disposable, archive, confirm=True)

        after = post_restore_check(clean_disposable)
        assert after["users"] == before["users"]
        assert after["ledger_entries"] == before["ledger_entries"]
        assert after["credits_outstanding"] == before["credits_outstanding"]
        assert after["billing_events"] == before["billing_events"]

        store = AccountStore(clean_disposable)
        try:
            # The balance is a view over the ledger, so this is the one
            # assertion that proves the rows arrived rather than a number.
            assert store.balance(seeded["id"]) == seeded["balance"]
        finally:
            store.close()

    def test_the_replay_guard_survives_a_restore(self, clean_disposable, tmp_path):
        """Losing `billing_events` restores a duplicate-payment bug: the next
        redelivered webhook grants a second time."""
        from formforge.accounts import AccountStore
        from formforge.backup import backup_postgres, restore_postgres

        _seed(clean_disposable)
        archive = backup_postgres(clean_disposable, tmp_path / "guard.dump")
        _drop_everything(clean_disposable)
        restore_postgres(clean_disposable, archive, confirm=True)

        store = AccountStore(clean_disposable)
        try:
            _, is_new = store.record_billing_event(
                "offline", "evt_rehearsal", "purchase"
            )
            assert not is_new, "a replayed webhook would have been applied again"
        finally:
            store.close()

    def test_migrations_are_a_no_op_on_a_restored_database(self, clean_disposable,
                                                           tmp_path):
        """Opening the store re-runs `migrate()`. On a database restored from
        the same build that should apply nothing -- if it applies something,
        the dump and the migration set disagree."""
        from formforge.accounts import AccountStore
        from formforge.backup import backup_postgres, restore_postgres

        _seed(clean_disposable)
        archive = backup_postgres(clean_disposable, tmp_path / "m.dump")
        _drop_everything(clean_disposable)
        restore_postgres(clean_disposable, archive, confirm=True)

        store = AccountStore(clean_disposable)
        try:
            assert store._db.migrate() == []
        finally:
            store.close()


class TestTheGuards:
    def test_a_restore_without_confirmation_does_nothing(self, clean_disposable,
                                                         tmp_path):
        from formforge.backup import BackupError, backup_postgres, restore_postgres

        _seed(clean_disposable)
        archive = backup_postgres(clean_disposable, tmp_path / "g.dump")
        with pytest.raises(BackupError, match="confirm"):
            restore_postgres(clean_disposable, archive, confirm=False)

    def test_the_dsn_password_does_not_reach_the_process_list(self, clean_disposable):
        """The property `split_dsn` exists for, checked against the DSN this
        run was actually given."""
        from formforge.backup import split_dsn

        safe, env = split_dsn(clean_disposable)
        if "PGPASSWORD" in env:
            assert env["PGPASSWORD"] not in safe
