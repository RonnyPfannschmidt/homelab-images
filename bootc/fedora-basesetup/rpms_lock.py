#!/usr/bin/env python3
"""Print `sha256 url` for every package in an rpms.lock.yaml, one per line.

Read by the Containerfile, which downloads each URL and checks it against the
hash beside it. Kept out of the Containerfile because a `RUN` with a python
one-liner in it is unreadable and unrunnable by hand; this is both.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import yaml


def host_arch() -> str:
    """The rpm architecture this build is for, as the lockfile spells it."""
    out = subprocess.run(["rpm", "-E", "%{_arch}"], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def main(path: str) -> int:
    arch = host_arch()
    with open(path) as fp:
        doc: dict[str, Any] = yaml.safe_load(fp)

    for entry in doc["arches"]:
        if entry["arch"] == arch:
            for pkg in entry["packages"]:
                print(pkg["checksum"].split(":", 1)[1], pkg["url"])
            return 0

    # Not a warning: a lockfile with no entry for the architecture being built
    # would otherwise install nothing at all and look like a success.
    print(f"{path}: no packages for {arch}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
