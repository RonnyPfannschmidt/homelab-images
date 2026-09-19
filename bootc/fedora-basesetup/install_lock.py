#!/usr/bin/env python3
"""Install exactly the packages rpms.lock.yaml names, and nothing else.

Every package is fetched by the URL in the lockfile and checked against the
hash beside it, then handed to dnf with every repository disabled. Nothing is
resolved at build time, so the package set is a property of a committed file
rather than of what Fedora served this morning.

The downloads live in a TemporaryDirectory that closes around the dnf call, so
they exist for exactly as long as the install needs them and no layer carries
them afterwards. That is also why the install happens here rather than back in
the Containerfile: a script that exited first would take the RPMs with it.

    ./install_lock.py rpms.lock.yaml
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

#: 85 files fetched one at a time, and one reset peer fails the whole build -
#: which is what the first CI run of this step did. Retried with a widening
#: gap rather than a fixed one, because a mirror that just refused a connection
#: is not ready again in the same second.
ATTEMPTS = 5
BACKOFF_SECONDS = 2
TIMEOUT_SECONDS = 60


def host_arch() -> str:
    """The rpm architecture this build is for, as the lockfile spells it."""
    done = subprocess.run(["rpm", "-E", "%{_arch}"], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def locked_packages(path: Path, arch: str) -> list[dict[str, Any]]:
    doc: dict[str, Any] = yaml.safe_load(path.read_text())
    for entry in doc["arches"]:
        if entry["arch"] == arch:
            packages: list[dict[str, Any]] = entry["packages"]
            return packages
    # Not a warning: a lockfile with no entry for the architecture being built
    # would otherwise install nothing at all and look like a success.
    raise SystemExit(f"{path}: no packages for {arch}")


def download(url: str, target: Path) -> None:
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with (
                urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response,
                target.open("wb") as handle,
            ):
                shutil.copyfileobj(response, handle)
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == ATTEMPTS:
                raise SystemExit(f"{url}: {exc}") from exc
            print(f"    {type(exc).__name__}, retrying ({attempt}/{ATTEMPTS - 1})")
            time.sleep(BACKOFF_SECONDS * attempt)


def verify(target: Path, expected: str) -> None:
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != expected:
        raise SystemExit(f"{target.name}: sha256 {digest}, lockfile says {expected}")


def main(lockfile: str) -> int:
    path = Path(lockfile)
    arch = host_arch()
    packages = locked_packages(path, arch)
    print(f"{len(packages)} packages for {arch} from {path.name}")

    with tempfile.TemporaryDirectory(prefix="rpms-") as workdir:
        staged: list[str] = []
        for package in packages:
            target = Path(workdir) / package["url"].rsplit("/", 1)[1]
            print(f"  {target.name}")
            download(package["url"], target)
            verify(target, package["checksum"].split(":", 1)[1])
            staged.append(str(target))

        subprocess.run(["dnf", "install", "-y", "--disablerepo=*", *staged], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
