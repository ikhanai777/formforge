"""Where generated files live, and how a browser is allowed to fetch one.

Two implementations behind one interface: a local filesystem store, which is
what the tests and a self-hosted deployment use, and an S3-compatible one for
a real deployment. The interface is deliberately tiny -- put, open, exists,
delete -- because everything above it should be indifferent to which is in use,
and a wide interface is how a local-only assumption leaks in.

**Nothing here is served as a static directory.** The local store's root is a
private path, never a web root, and the only way out of it is through
`GET /v1/download/...`, which checks a session or a signed token first. A
static mount would make every model in the store readable by anyone who can
guess a path, and model ids are exactly the kind of thing that ends up in a
log, a referrer header or a support email.

**On signed links.** `sign_download` mints a short-lived URL that carries its
own authorisation, for handing to a download manager or (on S3) to the bucket
directly. Being honest about what that is: a bearer credential. Anyone holding
the URL inside its window can fetch that one file. That is the same trade every
presigned-URL scheme makes, and the mitigations are the ones that matter --
it is unguessable without the signing key, it names exactly one model and one
format, it is bound to the account it was minted for, and it expires in
minutes rather than days. The session-authenticated route is the primary path;
this exists for the cases a cookie cannot reach.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Protocol

log = logging.getLogger("formforge.storage")

# Short on purpose. Long enough for a slow connection to start a large STL,
# short enough that a URL in a log or a chat message is stale by the time
# anyone reads it.
LINK_TTL_SECONDS = 300


class StorageError(Exception):
    pass


class Storage(Protocol):
    """Somewhere to keep generated artifacts."""

    def put(self, key: str, source: Path) -> str: ...
    def open(self, key: str) -> BinaryIO: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def size(self, key: str) -> int: ...


def artifact_key(model_id: str, fmt: str) -> str:
    """The key an artifact is stored under.

    Namespaced by model so a listing is per-model, and with no user id in it:
    a key is not an authorisation, and putting the owner in the path invites
    somebody to treat a path check as an ownership check.
    """
    return f"models/{model_id}/{fmt}"


def _safe(key: str) -> str:
    """Reject anything that could escape the store's root.

    A key reaches this from a model id and a format, both of which are
    generated rather than user-supplied -- but "currently unreachable" is not
    the same as "safe", and the cost of the check is nothing.
    """
    if not key or key.startswith("/") or ".." in key.split("/"):
        raise StorageError(f"unsafe storage key: {key!r}")
    return key


@dataclass
class LocalStorage:
    """Files under a private directory.

    `root` must not be inside anything a web server serves. Nothing here
    enforces that -- it cannot -- so it is stated in the docstring, in
    `docs/api-reference.md`, and again wherever the root is configured.
    """

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / _safe(key)).resolve()
        # Belt and braces against a key that survived `_safe`: the resolved
        # path has to still be under the root.
        if not str(path).startswith(str(self.root.resolve())):
            raise StorageError(f"key escapes the storage root: {key!r}")
        return path

    def put(self, key: str, source: Path) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, path)
        return key

    def open(self, key: str) -> BinaryIO:
        try:
            return self._path(key).open("rb")
        except FileNotFoundError as exc:
            raise StorageError(f"no such artifact: {key}") from exc

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def size(self, key: str) -> int:
        return self._path(key).stat().st_size


class S3Storage:
    """An S3-compatible bucket.

    Written against boto3 so it works with S3 itself, MinIO, R2, Spaces and
    anything else speaking the same API -- `endpoint_url` is what selects
    between them.

    **Unverified against a live bucket.** There is no S3 credential in the
    development environment, so this class has been exercised against the
    interface and not against a real endpoint. Treat it as needing a smoke
    test before it carries anything.
    """

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "",
        endpoint_url: str | None = None,
        client: Any = None,
    ):
        if client is None:
            import boto3

            client = boto3.client("s3", endpoint_url=endpoint_url)
        self._s3 = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def _key(self, key: str) -> str:
        safe = _safe(key)
        return f"{self.prefix}/{safe}" if self.prefix else safe

    def put(self, key: str, source: Path) -> str:
        self._s3.upload_file(str(source), self.bucket, self._key(key))
        return key

    def open(self, key: str) -> BinaryIO:
        try:
            response = self._s3.get_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:
            raise StorageError(f"no such artifact: {key}") from exc
        return response["Body"]

    def exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self.bucket, Key=self._key(key))
        except Exception:
            return False
        return True

    def delete(self, key: str) -> None:
        self._s3.delete_object(Bucket=self.bucket, Key=self._key(key))

    def size(self, key: str) -> int:
        return int(
            self._s3.head_object(Bucket=self.bucket, Key=self._key(key))["ContentLength"]
        )


# --------------------------------------------------------------------------
# Signed links
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DownloadGrant:
    """What a verified download token says its holder may fetch."""

    user_id: str
    model_id: str
    fmt: str
    expires_at: int


def sign_download(
    secret: str,
    *,
    user_id: str,
    model_id: str,
    fmt: str,
    ttl: int = LINK_TTL_SECONDS,
) -> str:
    """Mint a token for one account, one model and one format.

    All three are inside the signature, so a token cannot be edited into one
    for a different file or a different account -- which is the failure that
    would matter, and the reason none of them is a separate query parameter.
    """
    if not secret:
        raise StorageError("cannot sign a download link without a signing key")
    payload = {
        "u": user_id,
        "m": model_id,
        "f": fmt,
        "e": int(time.time()) + int(ttl),
    }
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
    mac = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return f"{body.decode().rstrip('=')}.{base64.urlsafe_b64encode(mac).decode().rstrip('=')}"


def verify_download(secret: str, token: str) -> DownloadGrant:
    """Check a token and say what it authorises. Raises otherwise.

    The signature is checked *before* the payload is parsed and the expiry is
    checked after -- an unsigned payload is attacker-controlled JSON and should
    not be interpreted at all, not even to read a timestamp out of it.
    """
    if not secret:
        raise StorageError("cannot verify a download link without a signing key")
    try:
        body, mac = token.split(".", 1)
    except (ValueError, AttributeError) as exc:
        raise StorageError("malformed download token") from exc

    body_bytes = body.encode()
    expected = hmac.new(secret.encode(), _unpad(body_bytes), hashlib.sha256).digest()
    try:
        given = base64.urlsafe_b64decode(_pad(mac.encode()))
    except Exception as exc:
        raise StorageError("malformed download token") from exc
    if not hmac.compare_digest(expected, given):
        raise StorageError("download token does not verify")

    payload = json.loads(base64.urlsafe_b64decode(_pad(body_bytes)))
    if int(payload.get("e", 0)) < time.time():
        raise StorageError("download link has expired")
    return DownloadGrant(
        user_id=str(payload["u"]),
        model_id=str(payload["m"]),
        fmt=str(payload["f"]),
        expires_at=int(payload["e"]),
    )


def _pad(value: bytes) -> bytes:
    return value + b"=" * (-len(value) % 4)


def _unpad(value: bytes) -> bytes:
    """The bytes the MAC was computed over.

    `sign_download` strips base64 padding for a tidier URL, so verification has
    to reconstruct exactly what was signed rather than what arrived.
    """
    return _pad(value)


def open_storage(target: str | None = None) -> Storage:
    """Pick a backend from a single setting.

    `s3://bucket/prefix` gets S3; anything else is a local path. One value
    rather than a driver name plus a location, for the same reason the account
    store takes one: two settings can disagree about where the data is.
    """
    if target is None:
        from .config import Settings

        target = Settings.from_env().artifacts
    if target.startswith("s3://"):
        rest = target[len("s3://"):]
        bucket, _, prefix = rest.partition("/")
        return S3Storage(
            bucket, prefix=prefix, endpoint_url=os.environ.get("FORMFORGE_S3_ENDPOINT")
        )
    return LocalStorage(Path(target))
