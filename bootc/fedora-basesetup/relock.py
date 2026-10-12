#!/usr/bin/env python3
"""Keep rpms.lock.yaml resolved against the base image the Containerfile names.

The lock is a function of two inputs: rpms.in.yaml and the base image's rpmdb.
Renovate bumps the second and cannot regenerate the lock - neither upstream
Renovate nor the hosted app has a manager for rpm-lockfile-prototype, and the
hosted app runs no postUpgradeTasks. A lock left behind breaks on any package
pinned to an exact base version: git requires `git-core = <its own EVR>`, so
the first base that ships a newer git-core makes the locked git uninstallable.

So the lock records the base it was resolved against, and `--check` fails the
moment the two disagree - in pre-commit, which pre-commit.ci runs on Renovate's
digest PR before the five-minute image build gets as far as dnf.

    ./relock.py            # regenerate; needs rpm-lockfile-prototype on PATH
    ./relock.py --check    # exit 1 if the lock was resolved against another base

rpm-lockfile-prototype needs the system's dnf bindings, so install it into a
venv that can see them:

    python3 -m venv --system-site-packages ~/.venvs/rlp
    ~/.venvs/rlp/bin/pip install git+https://github.com/konflux-ci/rpm-lockfile-prototype
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
CONTAINERFILE = HERE / "Containerfile"
INPUT = HERE / "rpms.in.yaml"
LOCK = HERE / "rpms.lock.yaml"

#: The key this script adds to the generated lock. rpm-lockfile-prototype does
#: not write it, and install_lock.py does not read it.
BASE_KEY = "baseImage"


def base_image() -> str:
    """The digest-pinned base, which is the last FROM: the first is `scratch`."""
    refs: list[str] = re.findall(r"^FROM\s+(\S+)", CONTAINERFILE.read_text(), re.MULTILINE)
    ref = refs[-1]
    if "@sha256:" not in ref:
        raise SystemExit(f"{CONTAINERFILE}: base {ref} is not pinned by digest")
    return ref


def locked_base() -> str | None:
    base: str | None = yaml.safe_load(LOCK.read_text()).get(BASE_KEY)
    return base


def check() -> int:
    want, have = base_image(), locked_base()
    if want == have:
        return 0
    print(
        f"{LOCK.name} was resolved against\n  {have or 'no recorded base'}\n"
        f"but {CONTAINERFILE.name} builds on\n  {want}\n"
        "Regenerate it: bootc/fedora-basesetup/relock.py",
        file=sys.stderr,
    )
    return 1


def relock() -> int:
    tool = shutil.which("rpm-lockfile-prototype")
    if tool is None:
        raise SystemExit("rpm-lockfile-prototype is not on PATH - see this script's docstring")
    ref = base_image()
    subprocess.run(
        [tool, "--image", ref, f"--outfile={LOCK}", str(INPUT)],
        check=True,
    )
    # Inserted as text rather than round-tripped through yaml.dump, so the
    # body stays byte-for-byte what rpm-lockfile-prototype wrote.
    text = LOCK.read_text()
    vendor = "lockfileVendor: redhat\n"
    if vendor not in text:
        raise SystemExit(f"{LOCK}: no `{vendor.strip()}` line to stamp the base after")
    LOCK.write_text(text.replace(vendor, f"{vendor}{BASE_KEY}: {ref}\n", 1))
    return check()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="verify only, change nothing")
    args = parser.parse_args()
    return check() if args.check else relock()


if __name__ == "__main__":
    sys.exit(main())
