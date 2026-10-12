#!/usr/bin/env python3
"""A synchronous OpenAI image API in front of Olares' Router.

Router serves FlowStudio workflows only asynchronously: a plain
``POST /v1/images/generations`` is refused with
``image_generation_async_required``, the prompt has to be placed under
``flowstudio.params`` by the workflow's own field name, and the picture is
collected from a ``content_url`` once the generation completes. Clients that
speak the ordinary OpenAI image API - Open WebUI's ``openai`` image engine
among them - send one request and wait for ``data[].b64_json``. This answers
that request by doing the asynchronous dance on the caller's behalf.

Standard library only, so the image has nothing to pin beyond its base.

Configuration is environment:

``ROUTER_URL``
    Router's OpenAI base, e.g. ``http://<hash>.shared.olares.com/v1``. Required.
``LISTEN_PORT``
    Default 8080.
``GENERATION_TIMEOUT``
    Seconds to wait for one generation before answering 504. Default 600,
    which is FlowStudio's own budget for bringing up an engine.
``POLL_INTERVAL``
    Seconds between polls. Default 2.
``MODELS_TTL``
    Seconds Router's model list is cached. Default 60.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger("routerimages")

#: Generation states Router reports while the work is still going.
PENDING = frozenset({"queued", "in_progress"})


class RouterError(Exception):
    """Router answered with an error; carried back to the caller as-is."""

    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(f"router answered {status}")
        self.status = status
        self.body = body


@dataclass
class Router:
    """The few calls this needs against Router's OpenAI surface."""

    base: str
    timeout: float = 60.0

    def _url(self, path: str) -> str:
        # content_url and Location are absolute paths that already carry /v1.
        if path.startswith("/v1/"):
            parts = urllib.parse.urlsplit(self.base)
            return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))
        return self.base.rstrip("/") + path

    def request(
        self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        data = None if body is None else json.dumps(body).encode()
        hdrs = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib.request.Request(
            self._url(path), data=data, method=method, headers=hdrs | (headers or {})
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            raise RouterError(e.code, e.read()) from None

    def json(
        self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
    ) -> tuple[dict[str, str], Any]:
        _, hdrs, raw = self.request(method, path, body, headers)
        return hdrs, json.loads(raw)


@dataclass
class ModelCatalog:
    """Router's ``/v1/models``, cached; only the rows that carry a contract matter."""

    router: Router
    ttl: float = 60.0
    clock: Callable[[], float] = time.monotonic
    _rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    _fetched: float = float("-inf")
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, model: str) -> dict[str, Any] | None:
        with self._lock:
            if model not in self._rows or self.clock() - self._fetched > self.ttl:
                _, listing = self.router.json("GET", "/models")
                self._rows = {row["id"]: row for row in listing.get("data", [])}
                self._fetched = self.clock()
            return self._rows.get(model)


def router_body(request: dict[str, Any], row: dict[str, Any] | None) -> dict[str, Any]:
    """The Router request for an OpenAI one, shaped to the model's contract.

    Router refuses a field a workflow has no parameter for, so only the fields
    in ``canonical_fields`` pass. The prompt is also written under the
    workflow's own name for it - Router does not map ``prompt`` onto a field
    called ``text`` (Z-Image Turbo), and answers ``params.text is required``.
    """
    if row is None or "flowstudio" not in row:
        return dict(request)

    canonical = set(row.get("canonical_fields") or ())
    body: dict[str, Any] = {"model": request["model"], "prompt": request["prompt"]}
    for name, key in (
        ("n", "n"),
        ("seed", "seed"),
        ("negative_prompt", "negative_prompt"),
        ("output.size", "size"),
    ):
        if name in canonical and request.get(key) not in (None, ""):
            body[key] = request[key]

    prompt_key = next(
        (
            p["key"]
            for p in row["flowstudio"].get("parameters", [])
            if p.get("associateRole") == "prompt"
        ),
        None,
    )
    if prompt_key and prompt_key != "prompt":
        body["flowstudio"] = {"params": {prompt_key: request["prompt"]}}
    return body


@dataclass
class Generator:
    router: Router
    catalog: ModelCatalog
    timeout: float = 600.0
    interval: float = 2.0
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    def generate(self, request: dict[str, Any]) -> dict[str, Any]:
        row = self.catalog.get(request["model"])
        body = router_body(request, row)
        if row is None or "flowstudio" not in row:
            # Not a workflow: Router answers these synchronously already.
            result: dict[str, Any] = self.router.json("POST", "/images/generations", body)[1]
            return result

        hdrs, job = self.router.json(
            "POST", "/images/generations", body, {"Prefer": "respond-async"}
        )
        location = (
            hdrs.get("location") or hdrs.get("Location") or f"/images/generations/{job['id']}"
        )
        deadline = self.clock() + self.timeout
        while job.get("status") in PENDING:
            if self.clock() > deadline:
                raise TimeoutError(job.get("id"))
            self.sleep(self.interval)
            _, job = self.router.json("GET", location)

        if job.get("status") != "completed":
            raise GenerationFailed(job)
        data = []
        for output in job.get("outputs", []):
            _, _, raw = self.router.request("GET", output["content_url"])
            data.append({"b64_json": base64.b64encode(raw).decode()})
        return {"created": int(time.time()), "data": data}


class GenerationFailed(Exception):
    def __init__(self, job: dict[str, Any]) -> None:
        super().__init__(job.get("error") or job.get("status"))
        self.job = job


def make_handler(generator: Generator) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, status: int, payload: Any) -> None:
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _error(self, status: int, message: str, code: str) -> None:
            self._send(
                status, {"error": {"message": message, "type": "routerimages_error", "code": code}}
            )

        def do_GET(self) -> None:
            if self.path in ("/", "/healthz"):
                self._send(HTTPStatus.OK, {"status": "ok"})
            else:
                self._error(HTTPStatus.NOT_FOUND, f"no route {self.path}", "not_found")

        def do_POST(self) -> None:
            if self.path.rstrip("/") != "/v1/images/generations":
                self._error(HTTPStatus.NOT_FOUND, f"no route {self.path}", "not_found")
                return
            try:
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                if not request.get("model") or not request.get("prompt"):
                    raise ValueError("model and prompt are required")
            except ValueError as e:
                self._error(HTTPStatus.BAD_REQUEST, str(e), "invalid_request")
                return
            try:
                self._send(HTTPStatus.OK, generator.generate(request))
            except RouterError as e:
                self._send(e.status, e.body)
            except GenerationFailed as e:
                log.warning("generation %s failed: %s", e.job.get("id"), e)
                self._error(
                    HTTPStatus.BAD_GATEWAY, str(e), e.job.get("error_code") or "generation_failed"
                )
            except TimeoutError as e:
                self._error(
                    HTTPStatus.GATEWAY_TIMEOUT,
                    f"generation {e} did not finish in time",
                    "generation_timeout",
                )
            except (OSError, KeyError, ValueError) as e:
                log.exception("talking to router")
                self._error(
                    HTTPStatus.BAD_GATEWAY, f"router unreachable or unreadable: {e}", "router_error"
                )

        def log_message(self, format: str, *args: Any) -> None:
            log.info("%s %s", self.address_string(), format % args)

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    router = Router(os.environ["ROUTER_URL"])
    generator = Generator(
        router,
        ModelCatalog(router, ttl=float(os.environ.get("MODELS_TTL", "60"))),
        timeout=float(os.environ.get("GENERATION_TIMEOUT", "600")),
        interval=float(os.environ.get("POLL_INTERVAL", "2")),
    )
    server = ThreadingHTTPServer(
        ("0.0.0.0", int(os.environ.get("LISTEN_PORT", "8080"))), make_handler(generator)
    )
    log.info("serving on %s, router %s", server.server_address, router.base)
    # PID 1 in a container gets no default SIGTERM action, so without this a
    # pod stop waits out its whole grace period.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server.serve_forever()


if __name__ == "__main__":
    main()
