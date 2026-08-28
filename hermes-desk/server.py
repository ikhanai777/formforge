#!/usr/bin/env python3
"""Serve the Hermes & Co. dashboard and forward its API calls to a Hermes gateway.

The dashboard is static files. This process exists for one reason: the
Hermes API server authenticates with a bearer token, and a token that
reaches the browser is a token in localStorage, in the devtools network
tab, and in any extension the user has installed. So the browser talks
to this process, this process holds ``API_SERVER_KEY``, and it adds the
Authorization header on the way out.

    HERMES_API_KEY=change-me-local-dev python3 hermes-desk/server.py

Then open http://127.0.0.1:8777 and set the Switchboard transport to
PROXY (the default).

Routing:
    /hermes/<path>   ->  <HERMES_URL>/<path>, with the bearer token added
    everything else  ->  a file under this directory

Server-sent events are streamed through unbuffered, so run event
streams and token deltas arrive as they are produced.

Environment:
    HERMES_URL       upstream gateway     (default http://127.0.0.1:8642)
    HERMES_API_KEY   API_SERVER_KEY value (default empty)
    HOST             bind address         (default 127.0.0.1)
    PORT             bind port            (default 8777)

Only stdlib is used, so this runs anywhere Python 3.9+ does.
"""

from __future__ import annotations

import os
import sys
import json
import socket
import urllib.error
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PREFIX = "/hermes"

HERMES_URL = os.environ.get("HERMES_URL", "http://127.0.0.1:8642").rstrip("/")
HERMES_API_KEY = os.environ.get("HERMES_API_KEY", "")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8777"))

# Only these request headers are forwarded upstream. Anything else the
# browser sends (cookies, origin, its own Authorization) is dropped, so
# the proxy cannot be used to smuggle credentials to the gateway.
FORWARD_REQUEST_HEADERS = {
    "content-type",
    "accept",
    "x-hermes-session-id",
    "x-hermes-session-key",
    "idempotency-key",
}

# Hop-by-hop headers must not be relayed back to the browser.
DROP_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "content-encoding",
    "content-length",
    "upgrade",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
}

MAX_BODY = 10 * 1024 * 1024  # the gateway's own limit; reject earlier


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ── static files ────────────────────────────────────────────

    def do_GET(self):  # noqa: N802  (stdlib naming)
        if self.path.startswith(PREFIX + "/") or self.path == PREFIX:
            return self.proxy("GET")
        return super().do_GET()

    def do_HEAD(self):  # noqa: N802
        if self.path.startswith(PREFIX):
            return self.proxy("HEAD")
        return super().do_HEAD()

    # ── proxied verbs ───────────────────────────────────────────

    def do_POST(self):    # noqa: N802
        return self.proxy("POST")

    def do_PATCH(self):   # noqa: N802
        return self.proxy("PATCH")

    def do_PUT(self):     # noqa: N802
        return self.proxy("PUT")

    def do_DELETE(self):  # noqa: N802
        return self.proxy("DELETE")

    def do_OPTIONS(self):  # noqa: N802
        # Same-origin in normal use; answered so a preflight never hangs.
        self.send_response(204)
        self.send_header("Allow", "GET, POST, PATCH, PUT, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ── the proxy itself ────────────────────────────────────────

    def proxy(self, method: str):
        if not self.path.startswith(PREFIX):
            return self.fail(404, "not_found", f"nothing is served at {self.path}")

        upstream = HERMES_URL + (self.path[len(PREFIX):] or "/")

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self.fail(413, "body_too_large", "request body exceeds 10 MB")
        body = self.rfile.read(length) if length else None

        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() in FORWARD_REQUEST_HEADERS
        }
        if HERMES_API_KEY:
            headers["Authorization"] = f"Bearer {HERMES_API_KEY}"

        req = urllib.request.Request(upstream, data=body, headers=headers, method=method)

        # An SSE request must never sit behind a read timeout: the stream
        # is idle between events by design.
        wants_stream = "text/event-stream" in (self.headers.get("Accept") or "")
        timeout = None if wants_stream else 120

        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as err:
            # The gateway's own error body is more useful than ours.
            return self.relay(err, method)
        except urllib.error.URLError as err:
            reason = getattr(err, "reason", err)
            return self.fail(
                502, "gateway_unreachable",
                f"cannot reach {HERMES_URL} ({reason}). Is `hermes gateway` running "
                f"with API_SERVER_ENABLED=true?",
            )
        except (TimeoutError, socket.timeout):
            return self.fail(504, "gateway_timeout", f"{HERMES_URL} did not answer in time")

        return self.relay(resp, method)

    def relay(self, resp, method: str):
        """Stream an upstream response back to the browser, chunk by chunk."""
        status = getattr(resp, "status", None) or resp.getcode()
        content_type = resp.headers.get("Content-Type", "")
        streaming = "text/event-stream" in content_type

        self.send_response(status)
        for key, value in resp.headers.items():
            if key.lower() not in DROP_RESPONSE_HEADERS:
                self.send_header(key, value)

        if streaming:
            # Length is unknown and must stay unknown; close-delimited.
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            if method != "HEAD":
                self.pump(resp)
            return

        payload = resp.read() if method != "HEAD" else b""
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def pump(self, resp):
        """Copy an event stream through with no buffering of our own."""
        try:
            while True:
                chunk = resp.read1(4096) if hasattr(resp, "read1") else resp.read(1)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the tab navigated away mid-stream
        finally:
            resp.close()

    def fail(self, status: int, code: str, message: str):
        """Answer in the gateway's own OpenAI-shaped error format."""
        payload = json.dumps({
            "error": {"message": message, "type": "proxy_error", "code": code}
        }).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # ── misc ────────────────────────────────────────────────────

    def end_headers(self):
        # The dashboard is a local tool; keep the browser from caching a
        # stale build between edits.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))


def main() -> int:
    handler = partial(Handler, directory=str(ROOT))
    try:
        httpd = ThreadingHTTPServer((HOST, PORT), handler)
    except OSError as err:
        print(f"cannot bind {HOST}:{PORT} — {err}", file=sys.stderr)
        return 1

    httpd.daemon_threads = True

    print("┌─────────────────────────────────────────────────────────")
    print("│  HERMES & CO. — agency floor")
    print(f"│  dashboard   http://{HOST}:{PORT}")
    print(f"│  gateway     {HERMES_URL}")
    print(f"│  bearer key  {'set (held server-side)' if HERMES_API_KEY else 'NOT SET — set HERMES_API_KEY'}")
    print("└─────────────────────────────────────────────────────────")
    if not HERMES_API_KEY:
        print("  note: without HERMES_API_KEY the gateway will answer 401 and the")
        print("        dashboard will fall back to its SIMULATION transport.")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nclosing the floor.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
