"""Backup, and the half nobody tests: restore.

The value of a backup is entirely in the restore, and a restore path that has
never run is a guess. So every test here goes the whole way round -- write
rows, take an archive, destroy the database, restore it, and read the rows back
*through the application's own store* rather than raw SQL, because a schema the
app cannot open is not a successful restore however many bytes arrived.

The other half is refusals. A restore is irreversible, so the tests that matter
most are the ones proving it does not happen: no confirmation, an archive from
a schema this build does not know, a truncated file.
"""

from __future__ import annotations

import gzip
import sqlite3
import tarfile

import pytest

from formforge.accounts import AccountStore
from formforge.backup import (
    BackupError,
    backup_local,
    migration_compatibility,
    post_restore_check,
    restore_local,
    split_dsn,
    verify_backup,
)


@pytest.fixture
def populated(tmp_path):
    """A database with money in it: a user, a purchase, a spend."""
    path = tmp_path / "accounts.db"
    store = AccountStore(path)
    user = store.create_user("keeper@example.com", "a-long-enough-password")
    store.grant(user["id"], 40, note="test purchase")
    store.spend(user["id"], 1, model_id="m-1")
    store.record_billing_event("offline", "evt_1", "purchase", user_id=user["id"])
    store.audit("account.created", user_id=user["id"], actor="test")
    store.close()
    return path


class TestTheRoundTrip:
    def test_a_restored_database_holds_what_the_original_did(self, populated, tmp_path):
        archive = tmp_path / "backup.tar.gz"
        before = post_restore_check(populated)

        backup_local(populated, archive)
        populated.unlink()  # the disaster

        restore_local(archive, populated, confirm=True)
        after = post_restore_check(populated)
        assert after == before

    def test_the_balance_survives_rather_than_being_recomputed_wrong(
        self, populated, tmp_path
    ):
        """The balance is a view over the ledger, so this proves the ledger
        rows came back, not a cached number."""
        store = AccountStore(populated)
        user = store.get_user_by_email("keeper@example.com")
        expected = store.balance(user["id"])
        store.close()
        assert expected == 3 + 40 - 1  # opening grant, purchase, one spend

        archive = tmp_path / "b.tar.gz"
        backup_local(populated, archive)
        populated.unlink()
        restore_local(archive, populated, confirm=True)

        store = AccountStore(populated)
        try:
            user = store.get_user_by_email("keeper@example.com")
            assert store.balance(user["id"]) == expected
        finally:
            store.close()

    def test_the_replay_guard_survives(self, populated, tmp_path):
        """`billing_events` is what stops a redelivered webhook granting
        twice. A restore that loses it restores a duplicate-payment bug."""
        archive = tmp_path / "b.tar.gz"
        backup_local(populated, archive)
        populated.unlink()
        restore_local(archive, populated, confirm=True)

        store = AccountStore(populated)
        try:
            # is_new False means the row survived: a replayed webhook is
            # still recognised as already handled.
            _, is_new = store.record_billing_event("offline", "evt_1", "purchase")
            assert not is_new
        finally:
            store.close()

    def test_a_backup_taken_while_the_database_is_open_is_consistent(
        self, populated, tmp_path
    ):
        """The reason this uses SQLite's backup API and not `shutil.copy`."""
        store = AccountStore(populated)
        try:
            archive = tmp_path / "hot.tar.gz"
            info = backup_local(populated, archive)
            assert info.rows["users"] == 1
            restored = tmp_path / "restored.db"
            restore_local(archive, restored, confirm=True)
            assert post_restore_check(restored)["users"] == 1
        finally:
            store.close()


class TestVerification:
    def test_it_reports_what_the_archive_holds(self, populated, tmp_path):
        info = backup_local(populated, tmp_path / "b.tar.gz")
        read_back = verify_backup(tmp_path / "b.tar.gz")
        assert read_back.rows == info.rows
        assert read_back.rows["credit_ledger"] == 3
        assert read_back.revisions == info.revisions
        assert "users=1" in read_back.summary

    def test_a_truncated_archive_is_caught_before_a_restore(self, populated, tmp_path):
        archive = tmp_path / "b.tar.gz"
        backup_local(populated, archive)
        blob = archive.read_bytes()
        archive.write_bytes(blob[: len(blob) // 2])
        with pytest.raises(BackupError):
            verify_backup(archive)

    def test_a_corrupt_database_inside_a_valid_archive_is_caught(self, tmp_path):
        """A tar that opens fine, holding a database that does not. Exactly
        what `PRAGMA integrity_check` is for."""
        work = tmp_path / "work"
        work.mkdir()
        (work / "accounts.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 900)
        (work / "formforge-backup.json").write_text('{"backend": "sqlite"}')
        archive = tmp_path / "bad.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(work / "accounts.db", arcname="accounts.db")
            tar.add(work / "formforge-backup.json", arcname="formforge-backup.json")
        with pytest.raises(BackupError):
            verify_backup(archive)

    def test_something_that_is_not_a_backup_is_refused_by_name(self, tmp_path):
        other = tmp_path / "notours.tar.gz"
        with gzip.open(other, "wb") as handle:
            handle.write(b"not a tar")
        with pytest.raises(BackupError):
            verify_backup(other)

    def test_a_tar_without_our_members_is_refused(self, tmp_path):
        stray = tmp_path / "stray.txt"
        stray.write_text("hello")
        archive = tmp_path / "empty.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(stray, arcname="stray.txt")
        with pytest.raises(BackupError, match="not a FormForge backup"):
            verify_backup(archive)

    def test_a_missing_archive_says_so(self, tmp_path):
        with pytest.raises(BackupError, match="no archive"):
            verify_backup(tmp_path / "nothing.tar.gz")

    def test_a_missing_database_says_so(self, tmp_path):
        with pytest.raises(BackupError, match="no database"):
            backup_local(tmp_path / "nothing.db", tmp_path / "out.tar.gz")


class TestMigrationCompatibility:
    def test_a_matching_schema_is_fine(self, populated, tmp_path):
        info = backup_local(populated, tmp_path / "b.tar.gz")
        compatible, reason = migration_compatibility(info)
        assert compatible
        assert "matches" in reason

    def test_an_archive_from_a_newer_schema_is_refused(self, populated, tmp_path):
        """The restore that appears to work and fails on the first query
        against a column this build does not know about."""
        archive = tmp_path / "b.tar.gz"
        backup_local(populated, archive)
        info = verify_backup(archive)
        from_the_future = type(info)(
            path=info.path, created_at=info.created_at, backend=info.backend,
            revisions=(*info.revisions, "9999_from_the_future"),
            rows=info.rows, formforge_version="99.0",
        )
        compatible, reason = migration_compatibility(from_the_future)
        assert not compatible
        assert "9999_from_the_future" in reason

    def test_an_older_archive_is_allowed_and_says_what_will_apply(self, populated,
                                                                  tmp_path):
        archive = tmp_path / "b.tar.gz"
        backup_local(populated, archive)
        info = verify_backup(archive)
        behind = type(info)(
            path=info.path, created_at=info.created_at, backend=info.backend,
            revisions=info.revisions[:1], rows=info.rows,
            formforge_version=info.formforge_version,
        )
        compatible, reason = migration_compatibility(behind)
        assert compatible
        assert "apply on the next start" in reason

    def test_restore_refuses_the_newer_archive_rather_than_warning(
        self, populated, tmp_path, monkeypatch
    ):
        archive = tmp_path / "b.tar.gz"
        backup_local(populated, archive)

        from formforge import backup as backup_module

        monkeypatch.setattr(
            backup_module, "migration_compatibility",
            lambda info: (False, "pretend it is from the future"),
        )
        with pytest.raises(BackupError, match="refusing to restore"):
            restore_local(archive, tmp_path / "target.db", confirm=True)


class TestNothingIsOverwrittenByAccident:
    def test_restore_over_an_existing_database_needs_confirmation(
        self, populated, tmp_path
    ):
        archive = tmp_path / "b.tar.gz"
        backup_local(populated, archive)
        with pytest.raises(BackupError, match="confirm"):
            restore_local(archive, populated, confirm=False)

    def test_the_refused_restore_left_the_database_untouched(self, populated, tmp_path):
        archive = tmp_path / "b.tar.gz"
        backup_local(populated, archive)
        before = populated.read_bytes()
        with pytest.raises(BackupError):
            restore_local(archive, populated, confirm=False)
        assert populated.read_bytes() == before

    def test_confirm_has_no_default(self):
        """Structural, not behavioural: a keyword defaulting to False is one
        refactor away from defaulting to True. Requiring the caller to write
        it means every call site shows the decision."""
        import inspect

        parameter = inspect.signature(restore_local).parameters["confirm"]
        assert parameter.default is inspect.Parameter.empty
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY

    def test_the_replaced_database_is_moved_aside_not_deleted(self, populated, tmp_path):
        """The second-worst outcome is finding out the archive was the wrong
        one after the original is gone."""
        archive = tmp_path / "b.tar.gz"
        backup_local(populated, archive)

        store = AccountStore(populated)
        store.create_user("later@example.com", "another-long-password")
        store.close()

        restore_local(archive, populated, confirm=True)
        displaced = list(tmp_path.glob("accounts.db.displaced-*"))
        assert len(displaced) == 1
        conn = sqlite3.connect(str(displaced[0]))
        try:
            count = conn.execute("SELECT count(*) FROM users").fetchone()[0]
        finally:
            conn.close()
        assert count == 2, "the pre-restore database was not preserved intact"
        assert post_restore_check(populated)["users"] == 1


class TestPostRestoreValidation:
    def test_it_reads_through_the_store_so_a_broken_view_is_caught(self, populated):
        """A `SELECT count(*)` on the tables would pass on a database whose
        `credit_balance` view is missing, and that view is what the paywall
        reads. Migrations do not repair it -- the revision is recorded as
        applied, so it is skipped -- which is exactly why the check has to
        look rather than trust."""
        conn = sqlite3.connect(str(populated))
        try:
            conn.execute("DROP VIEW credit_balance")
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(BackupError, match="not usable by this build"):
            post_restore_check(populated)

    def test_a_file_that_is_not_a_database_is_reported_not_raised_raw(self, tmp_path):
        broken = tmp_path / "broken.db"
        broken.write_bytes(b"SQLite format 3\x00" + b"\x00" * 900)
        with pytest.raises(BackupError):
            post_restore_check(broken)

    def test_it_names_the_backend_it_actually_opened(self, populated):
        """"Did I check the thing I meant to check" is the first question, and
        it used to have no answer: passing a PostgreSQL DSN through `Path()`
        silently opened a new SQLite file and reported zero for everything."""
        assert post_restore_check(populated)["backend"] == "sqlite"

    def test_a_dsn_is_not_mangled_into_a_path(self, tmp_path, monkeypatch):
        """The regression itself. `postgresql:///name` through `Path()`
        collapses to a relative path SQLite is happy to create."""
        monkeypatch.chdir(tmp_path)
        with pytest.raises(BackupError):
            post_restore_check("postgresql://nobody@127.0.0.1:1/nothing")
        assert not list(tmp_path.iterdir()), (
            "a failed check created a file; the DSN was treated as a path"
        )

    def test_it_reports_the_numbers_an_operator_compares(self, populated):
        result = post_restore_check(populated)
        assert result["users"] == 1
        assert result["ledger_entries"] == 3
        assert result["credits_outstanding"] == 42
        assert result["billing_events"] == 1
        assert result["revisions"]


class TestTheDsnNeverReachesAnArgumentVector:
    """`ps` is readable by every user on the box for as long as a dump runs."""

    def test_the_password_moves_to_the_environment(self):
        safe, env = split_dsn("postgresql://ff:hunter2@db.internal:5432/formforge")
        assert "hunter2" not in safe
        assert env["PGPASSWORD"] == "hunter2"
        assert safe == "postgresql://ff@db.internal:5432/formforge"

    def test_a_dsn_without_a_password_is_left_alone(self):
        dsn = "postgresql://ff@localhost/formforge"
        assert split_dsn(dsn) == (dsn, {})

    def test_key_value_dsns_too(self):
        safe, env = split_dsn("host=db dbname=formforge user=ff password=hunter2")
        assert "hunter2" not in safe
        assert env["PGPASSWORD"] == "hunter2"
        assert "dbname=formforge" in safe


class TestTheCommand:
    def test_create_verify_restore_check(self, populated, tmp_path, monkeypatch, capsys):
        from formforge.cli import main

        monkeypatch.setenv("FORMFORGE_ACCOUNTS_DB", str(populated))
        archive = tmp_path / "cli.tar.gz"

        assert main(["backup", "create", str(archive)]) == 0
        assert archive.exists()

        assert main(["backup", "verify", str(archive)]) == 0
        assert "usable FormForge backup" in capsys.readouterr().out

        # Without --confirm nothing happens, and it exits non-zero so a script
        # cannot mistake the refusal for a completed restore.
        assert main(["backup", "restore", str(archive)]) == 1
        assert "Nothing done" in capsys.readouterr().out

        assert main(["backup", "restore", str(archive), "--confirm"]) == 0
        assert main(["backup", "check"]) == 0
        assert "users" in capsys.readouterr().out

    def test_verify_exits_nonzero_on_a_bad_archive(self, tmp_path, capsys):
        from formforge.cli import main

        bad = tmp_path / "bad.tar.gz"
        bad.write_bytes(b"nope")
        assert main(["backup", "verify", str(bad)]) == 1

    def test_check_flags_an_empty_restore(self, tmp_path, monkeypatch, capsys):
        """Restoring the wrong archive and getting zero users should not exit
        zero; that is the failure an operator needs to see immediately."""
        from formforge.cli import main

        monkeypatch.setenv("FORMFORGE_ACCOUNTS_DB", str(tmp_path / "empty.db"))
        assert main(["backup", "check"]) == 1
        assert "no users" in capsys.readouterr().out
