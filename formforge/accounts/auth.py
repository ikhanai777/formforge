"""Passwords and session tokens.

Standard library only, and that is a deliberate constraint rather than
asceticism: this module is the one place in FormForge where getting the
details wrong is silently catastrophic, so it should be small enough to read
in one sitting and depend on nothing that can be swapped underneath it.

Two secrets live here and they are treated differently, because they are
different kinds of thing:

``passwords``
    Low entropy, chosen by a human, and reused across sites. The only defence
    that matters is making each guess expensive, so they go through scrypt with
    a per-password salt and a deliberate work factor.

``session tokens``
    256 bits from the system CSPRNG. There is nothing to guess, so a slow KDF
    would buy no security and would cost that latency on *every authenticated
    request*. They are stored as a plain SHA-256 digest -- fast, and still
    means a stolen database dump yields nothing replayable.

Applying scrypt to both would look more careful and would be worse.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

# scrypt's cost parameters. n=2^14 with r=8 needs 128*n*r = 16 MiB and lands
# around 50-100 ms on ordinary server hardware, which is the usual interactive
# target: slow enough that offline guessing hurts, fast enough that a login is
# not a visible pause. Raising `n` later is safe -- the parameters are stored
# in the hash string, so old hashes keep verifying under their own settings.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32

# Long enough that a rejected password is genuinely weak rather than merely
# short. Deliberately not a composition rule (one capital, one symbol): those
# push people towards `Password1!` and are worse than length alone.
MIN_PASSWORD_LENGTH = 10

SESSION_TTL = timedelta(days=30)


class AuthError(Exception):
    """A credential was rejected. Carries no detail about *which* part was
    wrong -- see `formforge.accounts.store` for why that matters."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# -- passwords -------------------------------------------------------------
def hash_password(password: str) -> str:
    """Hash a password for storage. Returns a self-describing string.

    The format is ``scrypt$n$r$p$salt_hex$key_hex``: the parameters travel with
    the hash so that raising the work factor later does not invalidate every
    existing account.
    """
    if not isinstance(password, str):
        raise AuthError("password must be a string")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    salt = secrets.token_bytes(SALT_BYTES)
    key = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=KEY_BYTES,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${key.hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    """Check a password against a stored hash. False, never an exception, for
    anything malformed.

    A user with no password set (`encoded` is None -- an OAuth identity, or a
    row created by an admin) verifies as False rather than as True, which is
    the failure direction that does not hand out accounts.
    """
    if not encoded or not isinstance(password, str):
        return False
    try:
        scheme, n, r, p, salt_hex, key_hex = encoded.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(key_hex)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError, MemoryError):
        # A hash we cannot parse or cannot afford to compute is a failed
        # verification, not a crash in the login path.
        return False
    return hmac.compare_digest(actual, expected)


def needs_rehash(encoded: str | None) -> bool:
    """Whether a stored hash was made with weaker parameters than current.

    Called after a *successful* verification, which is the only moment the
    plaintext is in hand to re-hash with. Without this, raising SCRYPT_N
    protects new accounts and leaves every existing one at the old cost
    forever.
    """
    if not encoded:
        return False
    try:
        scheme, n, r, p, _salt, _key = encoded.split("$")
    except ValueError:
        return True
    if scheme != "scrypt":
        return True
    return (int(n), int(r), int(p)) < (SCRYPT_N, SCRYPT_R, SCRYPT_P)


# -- session tokens --------------------------------------------------------
def new_session_token() -> tuple[str, str]:
    """A fresh session token and the digest to store for it.

    Returns ``(token, token_hash)``. The token goes to the client exactly once
    and is never written down here; the digest is what the database holds. If
    the two are ever stored together the whole scheme is pointless.
    """
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    """The stored form of a session token. See the module docstring for why
    this is a bare digest rather than a KDF."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def session_expiry(*, now: datetime | None = None, ttl: timedelta = SESSION_TTL) -> datetime:
    return (now or _now()) + ttl


def normalise_email(email: str) -> str:
    """Lower-cased and stripped, matching what `citext` does in Postgres.

    The SQLite side has no `citext`, so the normalisation has to happen in
    Python or the two dialects disagree about whether two signups are the same
    person -- and the way that failure presents is a duplicate account that the
    unique index was supposed to prevent.
    """
    if not isinstance(email, str):
        raise AuthError("email must be a string")
    cleaned = email.strip().lower()
    # Not an RFC-conformant check, and not trying to be: the only property
    # needed here is that it addresses somebody, which delivery proves and a
    # regex cannot.
    if "@" not in cleaned or cleaned.startswith("@") or cleaned.endswith("@"):
        raise AuthError(f"not an email address: {email!r}")
    return cleaned
