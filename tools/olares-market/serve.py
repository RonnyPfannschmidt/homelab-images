"""The market source as one small service: a git checkout, and two POSTs.

It serves what `build_market.py` generates, and it keeps that generated tree
in step with `main` by pulling the checkout when GitHub says something
changed. Two POST routes, and they have nothing to do with each other:

    POST /api/v1/applications/info   Olares asking for app records
    POST /hooks/github               GitHub saying a push happened

Everything else is a GET over files. **This file** imports nothing outside
the standard library - but the generator it shells out to parses YAML, so
the interpreter this runs under has to have PyYAML in it. That is not a
detail: `sys.executable` is what the build subprocess inherits, so starting
the service on a bare system interpreter produces a service that comes up
healthy and never completes a single build.

**The rebuild never happens on Olares' request path.** A hook signature is
checked, the response is sent, and the pull runs on a worker thread behind a
lock; the live tree is a symlink that is swapped once the new one is
complete. A hook that arrives mid-build sets a flag rather than queueing a
second build, and a hook that never arrives is caught by the poll interval.
So the worst case is staleness bounded by `--poll-seconds`, never a
half-written catalog or a 502 while GitHub is unreachable.

State, all under `--state-dir`:

    repo/            a normal git checkout of the charts repository
    builds/<sha>/    one generated tree per commit built
    current -> builds/<sha>
    webhook-secret   the shared secret, 0600, never in git

Run it under supervisord and point a web backend at `--port`; see
[the README](README.md) for the uberspace side.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

LOG = logging.getLogger("olares-market")

#: A hook body is a few KB of JSON and an app-ids list is smaller. Anything
#: larger is not a caller we have; read it and refuse rather than buffering
#: whatever arrives.
MAX_BODY_BYTES = 1 << 20

#: What the market answers with when the caller names no version. Olares
#: sends its own as `?version=`, and that is echoed back in preference.
DEFAULT_MARKET_VERSION = "1.12.6"

#: Builds kept beside the live one, so a bad chart can be rolled back by
#: moving the symlink rather than by waiting for a fixed commit.
BUILDS_KEPT = 5

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".tgz": "application/gzip",
}


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, timeout=300
    )
    return result.stdout.strip()


class Market:
    """The served tree, and the one operation that replaces it."""

    def __init__(self, state: Path, branch: str, python: str) -> None:
        self.state = state
        self.branch = branch
        self.python = python
        self.repo = state / "repo"
        self.builds = state / "builds"
        self.current = state / "current"
        #: Held for the whole of a rebuild. `pending` is what makes a burst
        #: of hooks one build: a caller that cannot take the lock records
        #: that another build is wanted and returns.
        self._lock = threading.Lock()
        self._pending = threading.Event()

    # -- what the handler reads ------------------------------------------

    def path(self, *parts: str) -> Path | None:
        """A file inside the live tree, or None if it is not one.

        `parts` comes from the URL, so the containment check is not a
        formality: `..` in an app name or a `fileName` would otherwise reach
        the checkout, the secret, or anything else this account can read.
        """
        root = self.current.resolve()
        candidate = root.joinpath(*parts).resolve()
        if not candidate.is_file() or not candidate.is_relative_to(root):
            return None
        return candidate

    def catalog(self) -> dict[str, Any]:
        catalog = self.path("catalog.json")
        if catalog is None:
            raise FileNotFoundError("no catalog.json in the current build")
        loaded: dict[str, Any] = json.loads(catalog.read_text())
        return loaded

    # -- the one write ---------------------------------------------------

    def request_sync(self) -> None:
        """Ask for a rebuild; coalesce with one already running."""
        self._pending.set()
        threading.Thread(target=self._drain, name="sync", daemon=True).start()

    def _drain(self) -> None:
        if not self._lock.acquire(blocking=False):
            # Another thread holds it and will see `pending` before it stops.
            return
        try:
            while self._pending.is_set():
                self._pending.clear()
                try:
                    self.sync()
                except Exception:
                    # A failed sync must not take the service down: the
                    # previous build is still live and still correct.
                    LOG.exception("sync failed; keeping the current build")
        finally:
            self._lock.release()

    def sync(self) -> str | None:
        """Fetch, rebuild if the commit moved, swap. Returns the new sha."""
        git(self.repo, "fetch", "--prune", "origin", self.branch)
        git(self.repo, "reset", "--hard", f"origin/{self.branch}")
        sha = git(self.repo, "rev-parse", "HEAD")

        built = self.builds / sha
        if built.is_dir() and self.current.is_symlink() and self.current.resolve() == built:
            LOG.info("already at %s", sha[:12])
            return None

        if not built.is_dir():
            staging = self.builds / f".{sha}.building"
            # The build script comes out of the checkout, so a pull that
            # changes the generator takes effect on the next build. A
            # subprocess rather than an import for exactly that reason: an
            # imported module would stay the one loaded at startup.
            subprocess.run(
                [
                    self.python,
                    str(self.repo / "tools" / "olares-market" / "build_market.py"),
                    "--out",
                    str(staging),
                    "--charts",
                    str(self.repo / "olares-apps"),
                ],
                check=True,
                timeout=900,
            )
            staging.replace(built)

        # A symlink swap is atomic only via rename; `Path.symlink_to` on an
        # existing name is not, and the window is a request that 404s.
        pointer = self.state / ".current.new"
        pointer.unlink(missing_ok=True)
        pointer.symlink_to(built)
        os.replace(pointer, self.current)
        LOG.info("serving %s", sha[:12])
        self._prune()
        return sha

    def _prune(self) -> None:
        live = self.current.resolve()
        builds = sorted(
            (p for p in self.builds.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in builds[BUILDS_KEPT:]:
            if stale != live:
                shutil.rmtree(stale, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "olares-market/1.0"
    market: Market
    webhook_secret: bytes

    # -- plumbing --------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:
        LOG.info("%s %s", self.address_string(), format % args)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _file(self, path: Path, content_type: str | None = None) -> None:
        body = path.read_bytes()
        self._send(
            HTTPStatus.OK,
            body,
            content_type or CONTENT_TYPES.get(path.suffix, "application/octet-stream"),
        )

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            # Refused without reading it, so the rest of the request is still
            # in flight and this connection cannot be reused - say so and
            # close. A client that is already writing may see the reset
            # instead of the 413; that is the right trade against buffering
            # whatever someone chose to send.
            self.close_connection = True
            raise ValueError(f"body of {length} bytes refused")
        return self.rfile.read(length)

    def _version(self, query: dict[str, list[str]]) -> str:
        return (query.get("version") or [DEFAULT_MARKET_VERSION])[0]

    # -- routing ---------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        # Olares 1.12.7+ asks under /api/v2/ first and falls back to /api/v1/
        # on a 404. Answering both saves the extra round trip.
        path = unquote(parsed.path).replace("/api/v2/", "/api/v1/", 1)
        query = parse_qs(parsed.query)

        try:
            if path in {"/health", "/healthz"}:
                catalog = self.market.catalog()
                self._json(
                    {
                        "status": "ok",
                        "apps": len(catalog["summaries"]),
                        "hash": catalog["hash"],
                        "build": self.market.current.resolve().name,
                    }
                )
                return

            if path == "/api/v1/appstore/hash":
                # Served rather than proxied: the file's `version` cannot
                # know what was asked for.
                self._json(
                    {
                        "hash": self.market.catalog()["hash"],
                        "last_updated": self._stored("api/v1/appstore/hash")["last_updated"],
                        "version": self._version(query),
                    }
                )
                return

            if path == "/api/v1/appstore/info":
                self._json(self._stored("api/v1/appstore/info") | {"version": self._version(query)})
                return

            chart = self._chart_path(path, query)
            if chart is not None:
                self._file(chart, "application/gzip")
                return

            served = self.market.path(*[part for part in path.split("/") if part]) or (
                self.market.path("index.html") if path == "/" else None
            )
            if served is not None:
                self._file(served)
                return

            self._json({"error": "Not Found", "path": path}, HTTPStatus.NOT_FOUND)
        except FileNotFoundError:
            # Only before the first build has landed.
            self._json({"error": "no build yet"}, HTTPStatus.SERVICE_UNAVAILABLE)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        path = unquote(urlparse(self.path).path).replace("/api/v2/", "/api/v1/", 1)
        try:
            body = self._body()
        except ValueError as error:
            self._json({"error": str(error)}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return

        if path == "/api/v1/applications/info":
            self._application_details(body)
            return
        if path == "/hooks/github":
            self._github_hook(body)
            return
        self._json({"error": "Not Found", "path": path}, HTTPStatus.NOT_FOUND)

    # -- the two POSTs ---------------------------------------------------

    def _application_details(self, body: bytes) -> None:
        """Olares asking for full records. The reason this is not static."""
        try:
            request = json.loads(body or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "invalid JSON"}, HTTPStatus.BAD_REQUEST)
            return

        details = self.market.catalog()["details"]
        wanted = request.get("app_ids") or []
        found = {app_id: details[app_id] for app_id in wanted if app_id in details}
        missing = [app_id for app_id in wanted if app_id not in details]
        self._json(
            {
                "apps": found,
                "version": request.get("version") or DEFAULT_MARKET_VERSION,
                **({"not_found": missing} if missing else {}),
            }
        )

    def _github_hook(self, body: bytes) -> None:
        """GitHub saying `main` moved. Signature first, work afterwards."""
        if not self.webhook_secret:
            # An unsigned route that pulls and runs a build script is a
            # remote code execution waiting for someone to find the URL.
            self._json({"error": "hook disabled"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return

        signature = self.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(self.webhook_secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            LOG.warning("rejected a hook with a bad signature")
            self._json({"error": "bad signature"}, HTTPStatus.FORBIDDEN)
            return

        event = self.headers.get("X-GitHub-Event", "")
        if event == "ping":
            self._json({"pong": True})
            return
        if event != "push":
            self._json({"ignored": event})
            return

        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "invalid JSON"}, HTTPStatus.BAD_REQUEST)
            return

        ref = payload.get("ref", "")
        if ref != f"refs/heads/{self.market.branch}":
            self._json({"ignored": ref})
            return

        # Answer before doing the work: GitHub gives a hook ten seconds, and
        # a build takes longer than that.
        self._json({"accepted": True, "ref": ref}, HTTPStatus.ACCEPTED)
        self.market.request_sync()

    # -- helpers ---------------------------------------------------------

    def _stored(self, relative: str) -> dict[str, Any]:
        path = self.market.path(*relative.split("/"))
        if path is None:
            raise FileNotFoundError(relative)
        loaded: dict[str, Any] = json.loads(path.read_text())
        return loaded

    def _chart_path(self, path: str, query: dict[str, list[str]]) -> Path | None:
        """`/api/v1/applications/<app>/chart`, optionally `?fileName=`."""
        parts = [part for part in path.split("/") if part]
        if len(parts) != 5 or parts[:3] != ["api", "v1", "applications"] or parts[4] != "chart":
            return None
        file_name = (query.get("fileName") or [""])[0]
        if file_name:
            # Named by the caller, so it goes through the containment check
            # in Market.path like any other URL-derived segment.
            return self.market.path("charts", file_name)
        return self.market.path("api", "v1", "applications", parts[3], "chart")


def poller(market: Market, seconds: int) -> None:
    """The safety net: a hook that was never delivered, or arrived while down."""
    while True:
        market.request_sync()
        threading.Event().wait(seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True, help="repo/, builds/, current")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--branch", default="main")
    parser.add_argument(
        "--secret-file",
        type=Path,
        default=None,
        help="the GitHub webhook secret; defaults to <state-dir>/webhook-secret",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=1800,
        help="fallback sync interval for missed hooks; 0 disables it",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    state: Path = args.state_dir
    market = Market(state, args.branch, sys.executable)
    if not (market.repo / ".git").is_dir():
        raise SystemExit(f"{market.repo} is not a git checkout; clone the charts repository there")
    market.builds.mkdir(parents=True, exist_ok=True)

    secret_file: Path = args.secret_file or (state / "webhook-secret")
    secret = secret_file.read_bytes().strip() if secret_file.is_file() else b""
    if not secret:
        LOG.warning("%s is missing or empty: the GitHub hook route is disabled", secret_file)

    Handler.market = market
    Handler.webhook_secret = secret

    # Build before listening: a market that answers with the previous tree is
    # fine, one that answers 503 because nothing was ever built is not.
    market.request_sync()
    if args.poll_seconds:
        threading.Thread(
            target=poller, args=(market, args.poll_seconds), name="poller", daemon=True
        ).start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    LOG.info("listening on %s:%s, state in %s", args.host, args.port, state)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
