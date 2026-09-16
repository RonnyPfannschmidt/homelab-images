#!/usr/bin/env python3
"""Move the pins in apps.lock, deliberately and one command at a time.

The build never runs this. apps.lock is the only thing a build reads, and it
is a committed file, so an app version changes when someone changes it here -
which is the whole reason the apps are baked into the image rather than pulled
from the app store at runtime.

    ./pin_apps.py --check              re-download every pin and verify its hash
    ./pin_apps.py --available          what the app store offers that is newer
    ./pin_apps.py --set memories=7.8.2 repin one app, or several
"""

from __future__ import annotations

import argparse
import hashlib
import json
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
        entry = index.get(pin.app)
        releases = (
            [] if entry is None else [r["version"] for r in entry["releases"] if not r["isNightly"]]
        )
        newest = max(releases, default="-")
        if newest != pin.version:
            print(f"{pin.app:24} {pin.version:10} -> {newest}")
    return 0


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
    group.add_argument("--check", action="store_true", help="verify every pinned hash")
    group.add_argument("--available", action="store_true", help="report newer releases")
    group.add_argument("--set", nargs="+", metavar="APP=VERSION", help="repin apps")
    args = parser.parse_args()

    header, pins = read_lock()
    if args.check:
        return cmd_check(pins)
    if args.available:
        return cmd_available(pins)
    return cmd_set(pins, header, args.set)


if __name__ == "__main__":
    sys.exit(main())
