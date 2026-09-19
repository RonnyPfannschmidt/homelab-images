"""The Nextcloud image's pins, checked against the app store that has to serve them.

`apps.lock` names an app and a version; the Containerfile names the Nextcloud
the image is built on. Nothing made the two agree until this file, and they
fail together in a way the build cannot see: the app store indexes by platform,
and an app whose `info.xml` caps below the server version is simply absent from
that platform's feed. The image still builds - the tarball URL keeps working -
and `occ upgrade` then disables the app and does not re-enable it. The failure
surfaces on the instance, after the bump.

`apps/nextcloud/README.md` already states the rule ("across a major version,
move `apps.lock` and the base digest in the same commit"). This is the rule
with a test behind it.

The tiers split the way the rest of the suite splits: parsing is free and runs
everywhere, anything that leaves the machine waits for `NEXTCLOUD_TESTS=1`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).parents[1]
NEXTCLOUD = REPO_ROOT / "apps" / "nextcloud"

# By path rather than as a module: it is a script beside the Containerfile, and
# duplicating its feed URL here is exactly the drift it avoids by reading the
# platform out of the `FROM` line.
_spec = importlib.util.spec_from_file_location("pin_apps", NEXTCLOUD / "pin_apps.py")
assert _spec and _spec.loader
pin_apps = importlib.util.module_from_spec(_spec)
# Registered before it is executed, not after: `@dataclass` resolves its own
# annotations through `sys.modules[cls.__module__]`, so a module that defines
# one dies on import with `'NoneType' object has no attribute '__dict__'`.
sys.modules[_spec.name] = pin_apps
_spec.loader.exec_module(pin_apps)

_HEADER, PINS = pin_apps.read_lock()
_IDS = [pin.app for pin in PINS]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@pytest.fixture(scope="session")
def store() -> dict[str, Any]:
    """The app store's index for the platform the Containerfile builds on."""
    if not os.environ.get("NEXTCLOUD_TESTS"):
        pytest.skip("set NEXTCLOUD_TESTS=1 to run; this queries the Nextcloud app store")
    index: dict[str, Any] = pin_apps.feed()
    return index


def test_apps_lock_pins_something() -> None:
    """A lock file that parses to nothing would pass every test below it."""
    assert PINS, "apps.lock names no apps"


def test_every_app_is_pinned_once() -> None:
    assert sorted(_IDS) == sorted(set(_IDS)), "apps.lock names an app twice"


@pytest.mark.parametrize("pin", PINS, ids=_IDS)
def test_pin_is_well_formed(pin: Any) -> None:
    assert _SHA256.match(pin.sha256), f"{pin.app}: {pin.sha256!r} is not a sha256"
    assert pin.url.startswith("https://"), f"{pin.app}: {pin.url} is not https"


@pytest.mark.parametrize("pin", PINS, ids=_IDS)
def test_pinned_version_is_offered_for_the_platform(pin: Any, store: dict[str, Any]) -> None:
    """The check a major bump needs, and the one the build cannot make.

    An absent app and a stale version are one failure with two shapes: either
    way the instance loses the app on the next `occ upgrade`.
    """
    platform = pin_apps.platform_version()
    entry = store.get(pin.app)
    assert entry is not None, (
        f"{pin.app} has no release at all for Nextcloud {platform}; "
        "it cannot be carried across this bump"
    )
    offered = [r["version"] for r in entry["releases"] if not r["isNightly"]]
    assert pin.version in offered, (
        f"{pin.app} {pin.version} is not offered for Nextcloud {platform}; "
        f"the store has {', '.join(sorted(offered)) or 'nothing'}"
    )


@pytest.mark.parametrize("pin", PINS, ids=_IDS)
def test_pinned_hash_still_matches(pin: Any, store: dict[str, Any]) -> None:
    """The tarball behind the URL is still the one that was pinned.

    A GitHub release asset can be replaced in place. The build verifies this
    too, but only after a runner has spent minutes getting there.
    """
    actual = hashlib.sha256(pin_apps.fetch(pin.url)).hexdigest()
    assert actual == pin.sha256, f"{pin.app} {pin.version}: {pin.url} now hashes to {actual}"
