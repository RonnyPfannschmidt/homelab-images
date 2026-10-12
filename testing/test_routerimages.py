"""The image facade against a Router that answers the way the live one did.

Every response shape below was recorded from Router on 2026-10-12 against
FlowStudio 0.3.87; the fake keeps them verbatim where it matters.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

# Loaded by path: apps/ is image sources, not a package. Registered before
# executing because dataclasses look their module up in sys.modules.
_SPEC = importlib.util.spec_from_file_location(
    "routerimages", Path(__file__).parents[1] / "apps" / "routerimages" / "routerimages.py"
)
assert _SPEC and _SPEC.loader
ri = importlib.util.module_from_spec(_SPEC)
sys.modules["routerimages"] = ri
_SPEC.loader.exec_module(ri)

ZIMAGE = {
    "id": "Olares/5b6fb2cc-24cc-4ddb-8c48-1ebe45db1c32",
    "mode": "image_generation",
    "canonical_fields": ["seed", "prompt", "output.size", "output.aspect_ratio", "n"],
    "flowstudio": {
        "parameters": [
            {"associateRole": "prompt", "key": "text", "type": "textarea", "required": True},
            {"key": "size", "type": "size", "default": "1024x1024"},
            {"key": "batch_count", "type": "batch", "default": 1},
        ]
    },
}
QWEN_IMAGE = {
    "id": "Olares/b088bea7-69aa-4c52-82de-bcdcbf17fdfa",
    "mode": "image_generation",
    "canonical_fields": ["seed", "prompt", "negative_prompt", "n"],
    "flowstudio": {
        "parameters": [{"associateRole": "prompt", "key": "prompt", "type": "textarea"}]
    },
}
CLOUD = {"id": "openai/gpt-image-1", "mode": "image_generation"}

PICTURE = b"RIFF\x00\x00\x00\x00WEBPVP8 fake"


class FakeRouter:
    """Router's answers, by method and path; records what was sent."""

    def __init__(self, polls: list[dict[str, Any]]) -> None:
        self.polls = list(polls)
        self.sent: list[tuple[str, str, Any, dict[str, str]]] = []

    def json(
        self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
    ) -> tuple[dict[str, str], Any]:
        _, hdrs, raw = self.request(method, path, body, headers)
        return hdrs, json.loads(raw)

    def request(
        self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        self.sent.append((method, path, body, headers or {}))
        if path == "/models":
            return 200, {}, json.dumps({"data": [ZIMAGE, QWEN_IMAGE, CLOUD]}).encode()
        if method == "POST" and path == "/images/generations":
            if (headers or {}).get("Prefer") != "respond-async" and body["model"] != CLOUD["id"]:
                raise ri.RouterError(400, b'{"error":{"code":"image_generation_async_required"}}')
            if body["model"] == CLOUD["id"]:
                return 200, {}, json.dumps({"data": [{"b64_json": "c3luYw=="}]}).encode()
            job = {"id": "gen_1", "object": "media.generation", "status": "queued"}
            return 202, {"location": "/v1/images/generations/gen_1"}, json.dumps(job).encode()
        if method == "GET" and path == "/v1/images/generations/gen_1":
            return 200, {}, json.dumps(self.polls.pop(0)).encode()
        if method == "GET" and path.startswith("/v1/generations/gen_1/content"):
            return 200, {"content-type": "image/webp"}, PICTURE
        raise AssertionError(f"unexpected {method} {path}")


COMPLETED = {
    "id": "gen_1",
    "status": "completed",
    "progress": 100,
    "outputs": [
        {
            "id": "ffc9",
            "content_type": "image",
            "content_url": "/v1/generations/gen_1/content?outputId=ffc9",
        }
    ],
}
FAILED = {
    "id": "gen_1",
    "status": "failed",
    "error_code": "upstream_generation_failed",
    "error": "CUDA out of memory. Tried to allocate 742.00 MiB.",
}


def generator(polls: list[dict[str, Any]], timeout: float = 600.0) -> tuple[Any, FakeRouter]:
    router = FakeRouter(polls)
    clock = iter(range(0, 10_000, 1)).__next__
    gen = ri.Generator(
        router, ri.ModelCatalog(router), timeout=timeout, sleep=lambda _: None, clock=clock
    )
    return gen, router


OPEN_WEBUI_REQUEST = {
    "model": ZIMAGE["id"],
    "prompt": "a red bicycle in the rain",
    "n": 1,
    "size": "1024x1024",
    "response_format": "b64_json",
}


def test_zimage_prompt_goes_under_its_own_field_name() -> None:
    body = ri.router_body(OPEN_WEBUI_REQUEST, ZIMAGE)
    assert body == {
        "model": ZIMAGE["id"],
        "prompt": "a red bicycle in the rain",
        "n": 1,
        "size": "1024x1024",
        "flowstudio": {"params": {"text": "a red bicycle in the rain"}},
    }


def test_fields_the_workflow_lacks_are_dropped() -> None:
    """Qwen-Image has no size; Router refuses a field without a parameter."""
    body = ri.router_body(OPEN_WEBUI_REQUEST | {"model": QWEN_IMAGE["id"]}, QWEN_IMAGE)
    assert body == {"model": QWEN_IMAGE["id"], "prompt": "a red bicycle in the rain", "n": 1}


def test_generation_is_polled_and_returned_as_b64() -> None:
    gen, router = generator([{"id": "gen_1", "status": "in_progress", "progress": 29}, COMPLETED])
    result = gen.generate(OPEN_WEBUI_REQUEST)
    assert [base64.b64decode(d["b64_json"]) for d in result["data"]] == [PICTURE]
    submit = next(s for s in router.sent if s[0] == "POST")
    assert submit[3] == {"Prefer": "respond-async"}


def test_failed_generation_raises_with_routers_message() -> None:
    gen, _ = generator([FAILED])
    with pytest.raises(ri.GenerationFailed, match="CUDA out of memory"):
        gen.generate(OPEN_WEBUI_REQUEST)


def test_generation_that_never_finishes_times_out() -> None:
    gen, _ = generator([{"id": "gen_1", "status": "queued"}] * 20, timeout=5)
    with pytest.raises(TimeoutError):
        gen.generate(OPEN_WEBUI_REQUEST)


def test_a_model_that_is_not_a_workflow_passes_through_synchronously() -> None:
    gen, router = generator([])
    result = gen.generate({"model": CLOUD["id"], "prompt": "x", "response_format": "b64_json"})
    assert result == {"data": [{"b64_json": "c3luYw=="}]}
    assert all(s[3] == {} for s in router.sent)


@pytest.fixture
def served(request: pytest.FixtureRequest) -> Iterator[str]:
    gen, _ = generator(request.param)
    server = ThreadingHTTPServer(("127.0.0.1", 0), ri.make_handler(gen))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def post(base: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        base + "/v1/images/generations",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer "},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.mark.parametrize("served", [[COMPLETED]], indirect=True)
def test_http_round_trip_as_open_webui_sends_it(served: str) -> None:
    status, body = post(served, OPEN_WEBUI_REQUEST)
    assert status == 200
    assert base64.b64decode(body["data"][0]["b64_json"]) == PICTURE


@pytest.mark.parametrize("served", [[FAILED]], indirect=True)
def test_http_failure_is_a_502_with_routers_code(served: str) -> None:
    status, body = post(served, OPEN_WEBUI_REQUEST)
    assert status == 502
    assert body["error"]["code"] == "upstream_generation_failed"


@pytest.mark.parametrize("served", [[]], indirect=True)
def test_http_missing_prompt_is_a_400(served: str) -> None:
    status, body = post(served, {"model": ZIMAGE["id"]})
    assert status == 400
    assert body["error"]["code"] == "invalid_request"
