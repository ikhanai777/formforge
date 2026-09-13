"""A small JSON/CSV-over-HTTPS client, and the free-tier budget it enforces.

Stdlib ``urllib`` rather than ``requests`` or ``httpx``: the engine makes a
handful of GETs per cycle against six endpoints, and a dependency that exists to
save four lines is a dependency to upgrade forever. ``urllib`` also picks up
``HTTPS_PROXY``/``SSL_CERT_FILE`` from the environment, which is what a sandboxed
or proxied deploy target needs.

The part worth reading is ``MonthlyBudget``. Spec section 3.1 picks providers on
their free tiers -- GoldAPI allows 500 requests a month -- and a recompute loop
that polls every five minutes will burn that in under two days and then fail
silently at the worst possible moment. So the budget is tracked on disk, spent
before the request rather than after the provider complains, and exhausting it
raises ``RateLimited`` so the caller fails over to the redundant feed instead of
going dark. Counting requests is cheaper than explaining a blind week.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import random
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import ProviderError, RateLimited

log = logging.getLogger("bullion.http")

USER_AGENT = "bullion/0.1 (+signal-engine)"
DEFAULT_TIMEOUT = 20.0
MAX_ATTEMPTS = 4
# Responses larger than this are a sign the query was wrong (an unfiltered news
# firehose, a full-history CSV), and reading them to the end wastes the cycle.
MAX_BYTES = 12 * 1024 * 1024


def _ssl_context() -> ssl.SSLContext:
    """Default verification, honouring an explicitly configured CA bundle.

    Never ``check_hostname = False``. A signal engine that accepts an
    unauthenticated price feed has a worse problem than a TLS error.
    """
    bundle = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if bundle and Path(bundle).exists():
        return ssl.create_default_context(cafile=bundle)
    return ssl.create_default_context()


@dataclass
class MonthlyBudget:
    """A disk-backed request counter for one provider, reset on the calendar month.

    Shared across processes by way of the file, which is good enough: the failure
    mode of a lost increment under concurrency is one extra request against a
    500-request allowance, and the failure mode of a lock is a hung cycle.
    """

    name: str
    limit: int
    path: Path
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def _period(self) -> str:
        now = datetime.now(timezone.utc)
        return f"{now.year:04d}-{now.month:02d}"

    @property
    def used(self) -> int:
        state = self._read()
        return int(state.get(self._period(), 0))

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def spend(self, count: int = 1) -> None:
        """Reserve ``count`` requests, or raise ``RateLimited``."""
        with self._lock:
            state = self._read()
            period = self._period()
            used = int(state.get(period, 0))
            if used + count > self.limit:
                raise RateLimited(
                    f"{self.name}: monthly budget of {self.limit} requests exhausted "
                    f"({used} used this month); falling back to another feed"
                )
            # Keep only the current period; the history is not interesting.
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps({period: used + count}))
            except OSError as exc:  # pragma: no cover - disk trouble
                log.warning("could not persist %s budget: %s", self.name, exc)


@dataclass
class HttpClient:
    """Retrying GET client for one provider.

    Retries only what retrying can fix -- 429, 5xx, timeouts and connection
    resets -- with full jitter, and never retries a 4xx that means the request was
    wrong. Retrying a 401 just spends the budget four times.
    """

    provider: str
    budget: MonthlyBudget | None = None
    timeout: float = DEFAULT_TIMEOUT
    max_attempts: int = MAX_ATTEMPTS
    # Minimum seconds between requests, for providers with a per-second cap
    # (Twelve Data's free tier allows 8/minute).
    min_interval: float = 0.0
    _last_request: float = 0.0

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        wait = self.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)

    def get_bytes(self, url: str, params: dict[str, Any] | None = None) -> bytes:
        query = {k: v for k, v in (params or {}).items() if v is not None}
        full = f"{url}?{urllib.parse.urlencode(query)}" if query else url
        if self.budget is not None:
            self.budget.spend(1)

        last: Exception | None = None
        context = _ssl_context()
        for attempt in range(1, self.max_attempts + 1):
            self._throttle()
            request = urllib.request.Request(full, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout, context=context
                ) as resp:
                    self._last_request = time.monotonic()
                    return resp.read(MAX_BYTES)
            except urllib.error.HTTPError as exc:
                self._last_request = time.monotonic()
                body = ""
                try:
                    body = exc.read(2048).decode("utf-8", "replace")
                except Exception:  # pragma: no cover - body already consumed
                    pass
                if exc.code == 429:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    if attempt == self.max_attempts:
                        raise RateLimited(
                            f"{self.provider}: rate limited ({body[:200]})",
                            float(retry_after)
                            if retry_after and retry_after.isdigit()
                            else None,
                        ) from exc
                    last = exc
                elif 500 <= exc.code < 600:
                    last = exc
                    if attempt == self.max_attempts:
                        raise ProviderError(
                            f"{self.provider}: HTTP {exc.code} after {attempt} attempts: "
                            f"{body[:200]}"
                        ) from exc
                else:
                    raise ProviderError(
                        f"{self.provider}: HTTP {exc.code} {exc.reason}: {body[:200]}"
                    ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
                if attempt == self.max_attempts:
                    raise ProviderError(
                        f"{self.provider}: {type(exc).__name__} after {attempt} attempts: {exc}"
                    ) from exc
            # Full jitter: 2^attempt seconds, randomised, so a provider coming back
            # from an outage does not get every deployment's retry in the same tick.
            backoff = min(16.0, 2.0**attempt) * random.random()
            log.debug(
                "%s: attempt %d failed (%s); retrying in %.1fs",
                self.provider,
                attempt,
                last,
                backoff,
            )
            time.sleep(backoff)

        raise ProviderError(f"{self.provider}: exhausted retries: {last}")

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        raw = self.get_bytes(url, params)
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ProviderError(
                f"{self.provider}: response was not JSON ({raw[:200]!r})"
            ) from exc

    def get_csv(self, url: str, params: dict[str, Any] | None = None) -> list[dict[str, str]]:
        raw = self.get_bytes(url, params)
        text = raw.decode("utf-8-sig", "replace")
        return list(csv.DictReader(io.StringIO(text)))
