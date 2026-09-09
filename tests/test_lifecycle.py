"""Artifact lifecycle, account closure, and the audit trail.

Three things that share a property: each one is about something *not* being
there any more, and each has a wrong version that looks fine until somebody
asks where their data went.

Retention is the sharpest of them. It is off by default and these tests hold
that line: a missing setting must mean "delete nothing", never "age zero".
"""

from __future__ import annotations

import pytest

from formforge.accounts import AuthError

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def user(accounts):
    return accounts.create_user("owner@example.com", PASSWORD)


class TestArtifactRecords:
    def test_recording_is_idempotent(self, accounts, user):
        """A rebuild or a retried write must not leave two rows for one file."""
        for _ in range(3):
            accounts.record_artifact("m1", "stl", "models/m1/stl", user_id=user["id"], size=10)
        rows = accounts.artifacts_for("m1")
        assert len(rows) == 1
        assert rows[0]["status"] == "present"

    def test_re_recording_revives_something_marked_for_deletion(self, accounts, user):
        """The bytes are back, so the mark is wrong."""
        accounts.record_artifact("m1", "stl", "k", user_id=user["id"], size=10)
        accounts.mark_artifacts("m1")
        assert accounts.artifacts_for("m1")[0]["status"] == "pending_delete"
        accounts.record_artifact("m1", "stl", "k", user_id=user["id"], size=10)
        row = accounts.artifacts_for("m1")[0]
        assert row["status"] == "present"
        assert row["marked_at"] is None

    def test_marking_is_the_soft_step_and_deletes_nothing(self, accounts, user):
        accounts.record_artifact("m1", "stl", "k", user_id=user["id"])
        assert accounts.mark_artifacts("m1") == 1
        assert [r["status"] for r in accounts.artifacts_for("m1")] == ["pending_delete"]
        assert len(accounts.artifacts_pending_delete()) == 1

    def test_marking_twice_marks_nothing_new(self, accounts, user):
        accounts.record_artifact("m1", "stl", "k", user_id=user["id"])
        accounts.mark_artifacts("m1")
        assert accounts.mark_artifacts("m1") == 0

    def test_the_row_survives_the_bytes(self, accounts, user):
        """The record that a file existed, and when it went, is what answers
        "where did my model go"."""
        accounts.record_artifact("m1", "stl", "k", user_id=user["id"])
        accounts.mark_artifacts("m1")
        row = accounts.artifacts_pending_delete()[0]
        accounts.finish_artifact_delete(row["id"])
        after = accounts.artifacts_for("m1")[0]
        assert after["status"] == "deleted"
        assert after["deleted_at"] is not None


class TestRetentionIsOff:
    def test_zero_days_selects_nothing(self, accounts, user):
        """The line that matters. A sweep treating a missing setting as age
        zero deletes everything."""
        accounts.record_artifact("old", "stl", "k", user_id=user["id"])
        assert accounts.artifacts_older_than(0) == []
        assert accounts.artifacts_older_than(-1) == []

    def test_a_paid_model_is_never_a_retention_candidate(self, accounts, user):
        """Deleting what somebody paid for on a timer is not a retention
        policy, it is a refund request."""
        accounts.record_artifact("paid", "stl", "k", user_id=user["id"])
        accounts.spend(user["id"], model_id="paid")
        assert accounts.artifacts_older_than(1, only_unpaid=True) == []

    def test_an_unpaid_model_becomes_a_candidate_once_old_enough(self, accounts, user):
        accounts.record_artifact("free", "stl", "k", user_id=user["id"])
        # Nothing is old yet, so even with retention on there is nothing to do.
        assert accounts.artifacts_older_than(30) == []
        # Backdate it past the window.
        with accounts._db.transaction() as conn:
            conn.execute(
                accounts._db.translate(
                    "UPDATE artifacts SET created_at = ? WHERE model_id = ?"
                ),
                (accounts._db.stamp(
                    __import__("datetime").datetime(2020, 1, 1, tzinfo=
                        __import__("datetime").timezone.utc)), "free"),
            )
        assert [r["model_id"] for r in accounts.artifacts_older_than(30)] == ["free"]

    def test_a_deleted_artifact_is_not_selected_again(self, accounts, user):
        accounts.record_artifact("gone", "stl", "k", user_id=user["id"])
        accounts.mark_artifacts("gone")
        accounts.finish_artifact_delete(accounts.artifacts_pending_delete()[0]["id"])
        assert accounts.artifacts_older_than(0) == []
        assert accounts.artifacts_pending_delete() == []


class TestAccountClosure:
    def test_a_closed_account_cannot_log_in(self, accounts, user):
        accounts.close_account(user["id"])
        with pytest.raises(AuthError):
            accounts.authenticate("owner@example.com", PASSWORD)

    def test_the_refusal_says_nothing_about_the_account(self, accounts, user):
        """"This account is closed" tells somebody probing addresses that one
        exists."""
        accounts.close_account(user["id"])
        closed = wrong = None
        try:
            accounts.authenticate("owner@example.com", PASSWORD)
        except AuthError as exc:
            closed = str(exc)
        try:
            accounts.authenticate("nobody@example.com", PASSWORD)
        except AuthError as exc:
            wrong = str(exc)
        assert closed == wrong

    def test_closing_revokes_every_session(self, accounts, user):
        tokens = [accounts.create_session(user["id"]) for _ in range(3)]
        assert accounts.close_account(user["id"]) == 3
        assert all(accounts.user_for_token(t) is None for t in tokens)

    def test_a_surviving_token_is_refused_anyway(self, accounts, user):
        """Belt and braces: "should be impossible" is how a closed account
        keeps working."""
        token = accounts.create_session(user["id"])
        with accounts._db.transaction() as conn:
            conn.execute(
                accounts._db.translate("UPDATE users SET closed_at = ? WHERE id = ?"),
                (accounts._db.now(), user["id"]),
            )
        assert accounts.user_for_token(token) is None

    def test_the_ledger_survives_closure(self, accounts, user):
        """It is a financial record. It outlives the account."""
        accounts.spend(user["id"], model_id="m1")
        accounts.close_account(user["id"])
        assert accounts.balance(user["id"]) == 2
        assert [e.reason for e in accounts.ledger(user["id"])] == ["spend", "grant"]

    def test_closure_is_reversible(self, accounts, user):
        accounts.close_account(user["id"])
        accounts.reopen_account(user["id"])
        assert accounts.authenticate("owner@example.com", PASSWORD)["email"] == (
            "owner@example.com"
        )

    def test_closing_twice_is_harmless(self, accounts, user):
        accounts.close_account(user["id"])
        accounts.close_account(user["id"])
        assert accounts.get_user(user["id"])["closed_at"] is not None


class TestAuditTrail:
    def test_closure_and_reopening_are_recorded(self, accounts, user):
        accounts.close_account(user["id"], actor="user")
        accounts.reopen_account(user["id"], actor="operator:alice")
        trail = accounts.audit_trail(user["id"])
        assert [(r["action"], r["actor"]) for r in trail] == [
            ("account.reopened", "operator:alice"),
            ("account.closed", "user"),
        ]

    def test_detail_round_trips(self, accounts, user):
        tokens = [accounts.create_session(user["id"]) for _ in range(2)]
        assert tokens
        accounts.close_account(user["id"])
        entry = accounts.audit_trail(user["id"])[0]
        assert entry["detail"]["sessions_revoked"] == 2

    def test_it_is_scoped_to_one_account(self, accounts, user):
        other = accounts.create_user("other@example.com", PASSWORD)
        accounts.close_account(user["id"])
        assert accounts.audit_trail(other["id"]) == []

    def test_an_audit_failure_never_breaks_the_thing_it_records(self, accounts, user):
        """Refusing a password reset because the audit row would not write is a
        worse outcome than an incomplete trail. Counted, not silent."""
        original = accounts._db.dumps

        def explode(_value):
            raise RuntimeError("audit backend is down")

        accounts._db.dumps = explode
        try:
            accounts.audit("something.happened", user_id=user["id"])
        finally:
            accounts._db.dumps = original
        assert accounts.audit_failures == 1

    def test_marking_artifacts_is_audited(self, accounts, user):
        accounts.record_artifact("m1", "stl", "k", user_id=user["id"])
        accounts.mark_artifacts("m1", actor="operator:bob")
        actions = [r["action"] for r in accounts.audit_trail(user["id"])]
        # Recorded against the operator rather than the account, so it is in
        # the global trail rather than this user's.
        assert "artifact.marked" not in actions
        with accounts._db.reader() as conn:
            rows = conn.execute("SELECT action FROM audit_log").fetchall()
        assert "artifact.marked" in {dict(r)["action"] for r in rows}


class TestMigrationsStillRepeatable:
    def test_the_third_revision_applies_once(self, accounts):
        assert accounts._db.migrate() == []

    def test_every_revision_is_recorded(self, accounts):
        with accounts._db.reader() as conn:
            applied = {dict(r)["id"] for r in conn.execute(
                "SELECT id FROM schema_migrations"
            ).fetchall()}
        assert applied == {"0001_accounts", "0002_password_resets", "0003_lifecycle"}
