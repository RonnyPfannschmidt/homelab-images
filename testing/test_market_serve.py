"""The service in front of the generated tree: routing, and the two POSTs.

Run against a real socket rather than by calling handler methods, because
what is worth asserting here is what a client sees - a status code, a
signature rejection, a file that is or is not reachable through a URL.

The sync half is exercised through `Market.request_sync` being called or not;
pulling a real repository belongs to the box, not to a unit test.
"""

from __future__ import annotations

import hashlib
import hmac
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
from conftest import REPO_ROOT

TOOLS = REPO_ROOT / "tools" / "olares-market"
SECRET = b"a-shared-secret"

#: Stands in for a commit sha; the build directory is named after one.
BUILD_SHA = "0" * 40


def load(name: str) -> Any:
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def serve() -> Any:
    return load("serve")


@pytest.fixture(scope="module")
def state(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A state directory holding one build, as a sync would have left it."""
    build_market = load("build_market")
    state = tmp_path_factory.mktemp("state")
    build = state / "builds" / BUILD_SHA
    assert build_market.main(["--out", str(build)]) == 0
    (state / "current").symlink_to(build)
    # Something outside the served tree, to aim a traversal at.
    (state / "webhook-secret").write_bytes(SECRET)
    return state


@pytest.fixture(scope="module")
def market(serve: Any, state: Path) -> Any:
    return serve.Market(state, "main", sys.executable)


@pytest.fixture(scope="module")
def base_url(serve: Any, market: Any) -> Iterator[str]:
    serve.Handler.market = market
    serve.Handler.webhook_secret = SECRET
    server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def post(url: str, body: bytes, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json", **(headers or {})}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def signed(body: bytes, secret: bytes = SECRET) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def test_the_three_get_endpoints_answer(base_url: str) -> None:
    status, body = get(f"{base_url}/api/v1/appstore/hash?version=1.12.6")
    assert status == 200
    hash_response = json.loads(body)
    assert hash_response["version"] == "1.12.6"

    status, body = get(f"{base_url}/api/v1/appstore/info?version=1.12.6")
    assert status == 200
    info = json.loads(body)
    assert info["hash"] == hash_response["hash"]
    assert info["data"]["apps"]


def test_the_version_is_echoed_rather_than_baked_in(base_url: str) -> None:
    """A static file cannot read a query string; this is why the app exists."""
    _, body = get(f"{base_url}/api/v1/appstore/hash?version=9.9.9")
    assert json.loads(body)["version"] == "9.9.9"


def test_the_v2_prefix_is_answered_too(base_url: str) -> None:
    """1.12.7+ asks under /api/v2/ first and only falls back on a 404."""
    assert get(f"{base_url}/api/v2/appstore/hash")[0] == 200


def test_a_chart_is_served_at_the_url_olares_asks_for(base_url: str, state: Path) -> None:
    catalog = json.loads((state / "current" / "catalog.json").read_text())
    app = next(iter(catalog["summaries"].values()))

    status, body = get(f"{base_url}/api/v1/applications/{app['name']}/chart")
    assert status == 200
    assert body[:2] == b"\x1f\x8b", "not gzip"

    named = f"{app['name']}-{app['version']}.tgz"
    status, by_name = get(f"{base_url}/api/v1/applications/{app['name']}/chart?fileName={named}")
    assert status == 200
    assert by_name == body


def test_a_filename_cannot_escape_the_served_tree(base_url: str) -> None:
    """`fileName` comes from the caller and is joined onto a path."""
    status, _ = get(
        f"{base_url}/api/v1/applications/anything/chart?fileName=../../../webhook-secret"
    )
    assert status == 404


def test_application_details_answers_by_id(base_url: str, state: Path) -> None:
    catalog = json.loads((state / "current" / "catalog.json").read_text())
    known = next(iter(catalog["details"]))

    status, body = post(
        f"{base_url}/api/v1/applications/info",
        json.dumps({"app_ids": [known, "deadbeef"], "version": "1.12.6"}).encode(),
    )
    assert status == 200
    answer = json.loads(body)
    assert set(answer["apps"]) == {known}
    assert answer["not_found"] == ["deadbeef"]


def test_an_unsigned_hook_is_refused(base_url: str, market: Any) -> None:
    """The hook pulls and runs a build script; an open route is an RCE."""
    body = json.dumps({"ref": "refs/heads/main"}).encode()
    assert post(f"{base_url}/hooks/github", body, {"X-GitHub-Event": "push"})[0] == 403
    assert (
        post(
            f"{base_url}/hooks/github",
            body,
            {"X-GitHub-Event": "push", "X-Hub-Signature-256": signed(body, b"wrong")},
        )[0]
        == 403
    )


def test_a_signed_push_to_main_schedules_a_sync(
    base_url: str, market: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = threading.Event()
    monkeypatch.setattr(market, "request_sync", called.set)

    body = json.dumps({"ref": "refs/heads/main"}).encode()
    status, _ = post(
        f"{base_url}/hooks/github",
        body,
        {"X-GitHub-Event": "push", "X-Hub-Signature-256": signed(body)},
    )
    # 202, not 200: GitHub gives a hook ten seconds and a build takes longer.
    assert status == 202
    assert called.wait(5)


def test_a_push_to_another_branch_is_ignored(
    base_url: str, market: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = threading.Event()
    monkeypatch.setattr(market, "request_sync", called.set)

    body = json.dumps({"ref": "refs/heads/some-branch"}).encode()
    status, answer = post(
        f"{base_url}/hooks/github",
        body,
        {"X-GitHub-Event": "push", "X-Hub-Signature-256": signed(body)},
    )
    assert status == 200
    assert json.loads(answer)["ignored"] == "refs/heads/some-branch"
    assert not called.is_set()


def test_a_ping_is_answered_so_the_hook_can_be_verified(base_url: str) -> None:
    body = json.dumps({"zen": "Non-blocking is better than blocking."}).encode()
    status, answer = post(
        f"{base_url}/hooks/github",
        body,
        {"X-GitHub-Event": "ping", "X-Hub-Signature-256": signed(body)},
    )
    assert status == 200
    assert json.loads(answer)["pong"] is True


def test_health_names_the_build_being_served(base_url: str) -> None:
    status, body = get(f"{base_url}/health")
    assert status == 200
    health = json.loads(body)
    assert health["apps"] > 0
    assert health["build"] == BUILD_SHA


def test_the_landing_page_is_served_at_the_root(base_url: str) -> None:
    status, body = get(f"{base_url}/")
    assert status == 200
    assert b"<title>" in body


def test_an_oversized_body_is_refused_rather_than_buffered(base_url: str, serve: Any) -> None:
    """Refused on Content-Length, before anything is read into memory.

    The client may see the 413 or may see the connection close under it -
    the server answered without draining a body it already refused, so the
    rest of the request is still in flight. Both are acceptable; what is not
    is the body being buffered, or the service being left unusable.
    """
    oversized = b"x" * (serve.MAX_BODY_BYTES + 1)
    try:
        status, _ = post(f"{base_url}/api/v1/applications/info", oversized)
        assert status == 413
    except (urllib.error.URLError, ConnectionError):
        pass
    assert get(f"{base_url}/health")[0] == 200
