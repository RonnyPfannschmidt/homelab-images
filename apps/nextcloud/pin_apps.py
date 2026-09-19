#!/usr/bin/env python3
"""Move the pins in apps.lock, deliberately and one command at a time.

The build never runs this. apps.lock is the only thing a build reads, and it
is a committed file, so an app version changes when someone changes it here -
which is the whole reason the apps are baked into the image rather than pulled
from the app store at runtime.

CI runs one of these, `--verify`, which writes nothing. Everything that moves
a pin stays a thing someone types.

    ./pin_apps.py --verify              does apps.lock match the Containerfile
    ./pin_apps.py --channel             is the pinned major still upstream's production
    ./pin_apps.py --check               re-download every pin and verify its hash
    ./pin_apps.py --available           what the app store offers that is newer
    ./pin_apps.py --set memories=7.8.2  repin one app, or several
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
LOCK = HERE / "apps.lock"
CONTAINERFILE = HERE / "Containerfile"

#: The app store indexes by platform, so the feed to ask depends on the base
#: image. Read it out of the FROM line rather than repeating it here, where the
#: two could drift apart without anything noticing.
_FROM_TAG = re.compile(r"^FROM\s+\S+?:(\d+\.\d+\.\d+)-apache", re.MULTILINE)

#: Upstream publishes its own opinion of what is ready for a live instance, as
#: a tag. It is not `latest`: 35.0.0 sat on `latest` for days while this still
#: served 34.0.4. renovate.json caps the major at whatever this says, and
#: `--channel` is what notices when the cap has gone stale.
PRODUCTION_TAG = "production-apache"
REPO = "library/nextcloud"
REGISTRY = "https://registry-1.docker.io"
TOKEN_URL = f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{REPO}:pull"

#: Both the OCI and the older Docker spellings, index and single manifest. Ask
#: for all four: which one comes back depends on how the tag was pushed, and a
#: request that names only one gets a 404 rather than a conversion.
MANIFEST_TYPES = (
    "application/vnd.oci.image.index.v1+json,"
    "application/vnd.docker.distribution.manifest.list.v2+json,"
    "application/vnd.oci.image.manifest.v1+json,"
    "application/vnd.docker.distribution.manifest.v2+json"
)


@dataclass(frozen=True)
class Pin:
    app: str
    version: str
    sha256: str
    url: str


def platform_version() -> str:
    """The Nextcloud version the Containerfile builds on."""
    match = _FROM_TAG.search(CONTAINERFILE.read_text())
    if match is None:
        raise SystemExit(f"{CONTAINERFILE}: no FROM nextcloud:<version>-apache line")
    return match.group(1)


def read_lock() -> tuple[list[str], list[Pin]]:
    """Return the leading comment block and the pins, so a rewrite keeps both."""
    header: list[str] = []
    pins: list[Pin] = []
    for line in LOCK.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            if not pins:
                header.append(line)
            continue
        app, version, sha256, url = line.split()
        pins.append(Pin(app, version, sha256, url))
    return header, pins


def write_lock(header: list[str], pins: list[Pin]) -> None:
    app_width = max(len(p.app) for p in pins)
    version_width = max(len(p.version) for p in pins)
    body = [
        f"{p.app:<{app_width}} {p.version:<{version_width}} {p.sha256} {p.url}"
        for p in sorted(pins, key=lambda p: p.app)
    ]
    LOCK.write_text("\n".join([*header, *body]) + "\n")


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url) as response:
        data: bytes = response.read()
    return data


def registry_get(url: str, token: str, accept: str | None = None) -> bytes:
    request = urllib.request.Request(url)
    request.add_header("Authorization", f"Bearer {token}")
    if accept:
        request.add_header("Accept", accept)
    with urllib.request.urlopen(request) as response:
        data: bytes = response.read()
    return data


def production_version() -> str:
    """The Nextcloud version upstream's own `production` tag serves today.

    Read out of the image's config rather than inferred from tag names: the
    floating `34` and `34.0` tags point at this image too, so a tag-name answer
    would be a major with no patch, and the patch is what the pin carries.
    """
    token: str = json.loads(fetch(TOKEN_URL))["token"]
    url = f"{REGISTRY}/v2/{REPO}/manifests/{PRODUCTION_TAG}"
    document: dict[str, Any] = json.loads(registry_get(url, token, MANIFEST_TYPES))

    if "manifests" in document:
        linux_amd64 = [
            entry
            for entry in document["manifests"]
            if entry.get("platform", {}).get("architecture") == "amd64"
            and entry.get("platform", {}).get("os") == "linux"
        ]
        if not linux_amd64:
            raise SystemExit(f"{PRODUCTION_TAG}: no linux/amd64 entry in the index")
        url = f"{REGISTRY}/v2/{REPO}/manifests/{linux_amd64[0]['digest']}"
        document = json.loads(registry_get(url, token, MANIFEST_TYPES))

    blob_url = f"{REGISTRY}/v2/{REPO}/blobs/{document['config']['digest']}"
    config: dict[str, Any] = json.loads(registry_get(blob_url, token))
    environment: list[str] = config["config"]["Env"]
    for item in environment:
        name, _, value = item.partition("=")
        if name == "NEXTCLOUD_VERSION":
            return value
    raise SystemExit(f"{PRODUCTION_TAG}: no NEXTCLOUD_VERSION in the image config")


def single_top_level_dir(payload: bytes) -> str | None:
    """The one directory a release tarball unpacks to, or None if it is not one.

    Nextcloud resolves an app by the directory name on disk, so a tarball that
    unpacks to anything but ``<app>/`` cannot be baked in unattended - and two
    of these upstreams name the asset ``release.tar.gz``, which is exactly the
    case where the assumption would go unchecked.
    """
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as handle:
        handle.write(payload)
        handle.flush()
        with tarfile.open(handle.name) as archive:
            tops = {member.name.split("/")[0] for member in archive.getmembers()}
    return tops.pop() if len(tops) == 1 else None


def feed() -> dict[str, Any]:
    url = f"https://apps.nextcloud.com/api/v1/platform/{platform_version()}/apps.json"
    apps: list[dict[str, Any]] = json.loads(fetch(url))
    return {app["id"]: app for app in apps}


def stable_key(version: str) -> tuple[int, ...] | None:
    """Sortable form of `version`, or None if it is not a stable release.

    The store lists release candidates and betas alongside releases, and they
    sort *above* the release they precede under a plain string compare -
    "6.6.0-rc.2" > "6.5.4". Anything with a suffix is dropped rather than
    ordered, because no suggestion this script makes should be a prerelease.
    """
    base, _, prerelease = version.partition("-")
    if prerelease:
        return None
    try:
        return tuple(int(part) for part in base.split("."))
    except ValueError:
        return None


def newest_stable(versions: list[str]) -> str | None:
    ranked = [(key, v) for v in versions if (key := stable_key(v)) is not None]
    return max(ranked)[1] if ranked else None


def offered(app: str, index: dict[str, Any]) -> list[str] | None:
    """Every non-nightly version of `app` this platform's feed carries.

    None means the app is not in the feed at all, which is a different problem
    from being in it at the wrong version: the app declares a `max-version`
    below this server in its info.xml, so there is no version to move to.
    """
    entry = index.get(app)
    if entry is None:
        return None
    return [release["version"] for release in entry["releases"] if not release["isNightly"]]


def resolve(app: str, version: str, index: dict[str, Any]) -> Pin:
    entry = index.get(app)
    if entry is None:
        raise SystemExit(f"{app}: not offered for Nextcloud {platform_version()}")
    for release in entry["releases"]:
        if release["version"] == version and not release["isNightly"]:
            url = str(release["download"])
            payload = fetch(url)
            top = single_top_level_dir(payload)
            if top != app:
                raise SystemExit(f"{app} {version}: tarball unpacks to {top!r}, not {app!r}")
            return Pin(app, version, hashlib.sha256(payload).hexdigest(), url)
    offered = ", ".join(sorted(r["version"] for r in entry["releases"]))
    raise SystemExit(f"{app}: no release {version}; the store offers {offered}")


def cmd_check(pins: list[Pin]) -> int:
    bad = 0
    for pin in pins:
        actual = hashlib.sha256(fetch(pin.url)).hexdigest()
        ok = actual == pin.sha256
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {pin.app} {pin.version}")
        if not ok:
            print(f"     pinned {pin.sha256}\n     actual {actual}")
    return 1 if bad else 0


def cmd_available(pins: list[Pin]) -> int:
    index = feed()
    for pin in pins:
        releases = offered(pin.app, index)
        newest = "-" if releases is None else (newest_stable(releases) or "-")
        if newest != pin.version:
            print(f"{pin.app:24} {pin.version:10} -> {newest}")
    return 0


def lock_lines() -> dict[str, int]:
    """Which line of apps.lock each app sits on, for editor and CI annotations."""
    numbers = {}
    for number, line in enumerate(LOCK.read_text().splitlines(), start=1):
        if line.strip() and not line.lstrip().startswith("#"):
            numbers[line.split()[0]] = number
    return numbers


def cmd_verify(pins: list[Pin]) -> int:
    """Does apps.lock still match the Nextcloud the Containerfile builds on.

    The build cannot answer this. It fetches each app by URL and checks its
    hash, and both still succeed for an app the new server will refuse to
    enable - the tarball has not changed, the app's info.xml simply caps below
    the server version, and `occ upgrade` disables it on the instance.
    """
    platform = platform_version()
    index = feed()
    annotate = bool(os.environ.get("GITHUB_ACTIONS"))
    lines = lock_lines() if annotate else {}

    width = max(len(p.app) for p in pins)
    stale: list[tuple[Pin, str | None]] = []
    gone: list[Pin] = []

    print(f"apps.lock against Nextcloud {platform}\n")
    for pin in pins:
        releases = offered(pin.app, index)
        if releases is None:
            gone.append(pin)
            state, note = "gone", f"no release for Nextcloud {platform}"
            detail = f"{pin.app} has no Nextcloud {platform} release at all"
        elif pin.version in releases:
            state, note, detail = "ok", "", ""
        else:
            newest = newest_stable(releases)
            stale.append((pin, newest))
            state = "stale"
            note = f"offered: {newest}" if newest else "only prereleases offered"
            detail = f"{pin.app} {pin.version} is not offered for Nextcloud {platform}" + (
                f"; {newest} is" if newest else "; only prereleases are"
            )
        print(f"  {pin.app:<{width}}  {pin.version:<8}  {state:<5}  {note}".rstrip())
        if detail and annotate:
            where = f"file={LOCK.relative_to(HERE.parents[1])},line={lines.get(pin.app, 1)}"
            print(f"::error {where}::{detail}")

    if not stale and not gone:
        print(f"\n{len(pins)} apps, all offered for Nextcloud {platform}.")
        return 0

    counts = [f"{len(stale)} stale" if stale else "", f"{len(gone)} gone" if gone else ""]
    print("\n" + ", ".join(c for c in counts if c) + f" for Nextcloud {platform}.")
    if stale:
        moves = " ".join(f"{pin.app}={newest}" for pin, newest in stale if newest)
        if moves:
            print("\nMove these with the base digest, in the same commit:\n")
            print(f"  ./pin_apps.py --set {moves}")
    for pin in gone:
        print(
            f"\n{pin.app} has no Nextcloud {platform} release at all. Drop it from "
            "apps.lock, or hold the platform here until it has one."
        )
    return 1


def cmd_channel() -> int:
    """Is the major this image pins still the one upstream calls production.

    renovate.json caps nextcloud below the next major, which is what keeps a
    `.0` off a live instance. A cap raised by hand has exactly one failure
    mode - nobody raises it - and this is what removes it: the day upstream
    promotes the next major to `production`, this goes red, and raising the cap
    becomes a deliberate act with a database migration attached rather than a
    Renovate PR that landed nine months early.
    """
    pinned = platform_version()
    upstream = production_version()
    pinned_major = pinned.split(".")[0]
    upstream_major = upstream.split(".")[0]

    print(f"pinned      {pinned}")
    print(f"production  {upstream}  (docker.io/{REPO}:{PRODUCTION_TAG})")

    if pinned_major == upstream_major:
        print(f"\nBoth on major {pinned_major}; the cap in renovate.json still fits.")
        return 0

    detail = (
        f"production now serves {upstream}; the image pins {pinned}. Raise the "
        f"nextcloud cap in renovate.json to <{int(upstream_major) + 1} and move "
        f"the base, or hold deliberately and say why here."
    )
    print(f"\n{detail}")
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::error file=renovate.json::{detail}")
    return 1


def cmd_set(pins: list[Pin], header: list[str], assignments: list[str]) -> int:
    index = feed()
    by_app = {p.app: p for p in pins}
    for assignment in assignments:
        app, _, version = assignment.partition("=")
        if app not in by_app:
            raise SystemExit(f"{app}: not in {LOCK.name}; add it by hand first")
        by_app[app] = resolve(app, version, index)
        print(f"{app} -> {version}")
    write_lock(header, list(by_app.values()))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--verify", action="store_true", help="check every pin against the platform's feed"
    )
    group.add_argument("--check", action="store_true", help="verify every pinned hash")
    group.add_argument(
        "--channel",
        action="store_true",
        help="check the pinned major is still upstream's production channel",
    )
    group.add_argument("--available", action="store_true", help="report newer releases")
    group.add_argument("--set", nargs="+", metavar="APP=VERSION", help="repin apps")
    args = parser.parse_args()

    header, pins = read_lock()
    if args.verify:
        return cmd_verify(pins)
    if args.check:
        return cmd_check(pins)
    if args.channel:
        return cmd_channel()
    if args.available:
        return cmd_available(pins)
    return cmd_set(pins, header, args.set)


if __name__ == "__main__":
    sys.exit(main())
