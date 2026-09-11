"""Artifact storage and signed download links.

The interesting failures here are all about someone fetching a file they were
not given: a token edited to name a different model, a token that has expired,
a key that climbs out of the storage root.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from formforge.storage import (
    LocalStorage,
    StorageError,
    artifact_key,
    open_storage,
    sign_download,
    verify_download,
)

SECRET = "a-signing-key"


@pytest.fixture
def store(tmp_path) -> LocalStorage:
    return LocalStorage(tmp_path / "artifacts")


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "model.stl"
    path.write_bytes(b"solid model")
    return path


class TestLocalStorage:
    def test_a_file_round_trips(self, store, source):
        key = artifact_key("model-1", "stl")
        store.put(key, source)
        assert store.exists(key)
        with store.open(key) as handle:
            assert handle.read() == b"solid model"
        assert store.size(key) == len(b"solid model")

    def test_a_missing_artifact_is_an_error_not_an_empty_file(self, store):
        assert not store.exists("models/nope/stl")
        with pytest.raises(StorageError):
            store.open("models/nope/stl")

    @pytest.mark.parametrize(
        "key", ["../escape", "models/../../etc/passwd", "/etc/passwd", ""]
    )
    def test_a_key_cannot_climb_out_of_the_root(self, store, key):
        """Keys are generated rather than user-supplied today. "Currently
        unreachable" is not "safe", and the check costs nothing."""
        with pytest.raises(StorageError):
            store.open(key)

    def test_delete_is_idempotent(self, store, source):
        key = artifact_key("model-1", "stl")
        store.put(key, source)
        store.delete(key)
        store.delete(key)
        assert not store.exists(key)

    def test_the_backend_is_chosen_by_one_setting(self, tmp_path):
        assert isinstance(open_storage(str(tmp_path)), LocalStorage)


class TestSignedLinks:
    def test_a_token_names_one_account_one_model_and_one_format(self):
        token = sign_download(SECRET, user_id="u1", model_id="m1", fmt="stl")
        grant = verify_download(SECRET, token)
        assert (grant.user_id, grant.model_id, grant.fmt) == ("u1", "m1", "stl")

    def test_a_token_signed_with_another_key_does_not_verify(self):
        token = sign_download(SECRET, user_id="u1", model_id="m1", fmt="stl")
        with pytest.raises(StorageError):
            verify_download("a-different-key", token)

    def test_an_edited_token_does_not_verify(self):
        """The failure that would matter: turning a token for your own model
        into one for somebody else's. All three fields are inside the MAC."""
        token = sign_download(SECRET, user_id="u1", model_id="m1", fmt="stl")
        body, mac = token.split(".", 1)
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        payload["m"] = "someone-elses-model"
        forged = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        with pytest.raises(StorageError):
            verify_download(SECRET, f"{forged}.{mac}")

    def test_an_expired_token_is_refused(self):
        token = sign_download(SECRET, user_id="u1", model_id="m1", fmt="stl", ttl=-1)
        with pytest.raises(StorageError, match="expired"):
            verify_download(SECRET, token)

    def test_a_token_with_a_forged_expiry_does_not_verify(self):
        """The expiry is inside the signature, so extending it breaks the MAC.
        Checking the signature before parsing the payload is what makes that
        true -- an unsigned payload is attacker-controlled JSON."""
        token = sign_download(SECRET, user_id="u1", model_id="m1", fmt="stl", ttl=-1)
        body, mac = token.split(".", 1)
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        payload["e"] = int(time.time()) + 100_000
        forged = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        with pytest.raises(StorageError, match="does not verify"):
            verify_download(SECRET, f"{forged}.{mac}")

    @pytest.mark.parametrize("junk", ["", "nonsense", "a.b.c", "....", "x."])
    def test_malformed_tokens_are_refused_without_crashing(self, junk):
        with pytest.raises(StorageError):
            verify_download(SECRET, junk)

    def test_signing_without_a_key_is_refused(self):
        """Better to fail loudly than to mint links anyone can forge."""
        with pytest.raises(StorageError):
            sign_download("", user_id="u1", model_id="m1", fmt="stl")
