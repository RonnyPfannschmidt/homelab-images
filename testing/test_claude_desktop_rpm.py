"""The apt-index handling in `tools/claude-desktop-rpm/build.py`.

The tool installs a root-owned binary with a setuid sandbox on the strength of
these checks, and none of them can be exercised against the live repository in
CI without downloading 180 MB. So the parsing and the refusals are held here,
offline.
"""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import subprocess
import sys
from typing import Any

import pytest
from conftest import REPO_ROOT

TOOL = REPO_ROOT / "tools" / "claude-desktop-rpm" / "build.py"


def load_tool() -> Any:
    spec = importlib.util.spec_from_file_location("claude_desktop_rpm_build", TOOL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


build = load_tool()

PACKAGES = """\
Package: claude-desktop
Version: 1.17180.0
Architecture: amd64
Depends: libgtk-3-0,
 libnss3
Filename: pool/main/c/claude-desktop/claude-desktop_1.17180.0_amd64.deb
Size: 10
SHA256: aaaa

Package: claude-desktop
Version: 2.9.0
Architecture: amd64
Filename: pool/main/c/claude-desktop/claude-desktop_2.9.0_amd64.deb
Size: 30
SHA256: cccc

Package: claude-desktop
Version: 2.10.0
Architecture: amd64
Filename: pool/main/c/claude-desktop/claude-desktop_2.10.0_amd64.deb
Size: 20
SHA256: bbbb

Package: something-else
Version: 9.0.0
Filename: pool/main/s/something-else/something-else_9.0.0_amd64.deb
Size: 1
SHA256: dddd
"""

RELEASE = """\
Origin: Anthropic
Suite: stable
SHA256:
 1111 53580 main/binary-amd64/Packages
 2222 6306 main/binary-amd64/Packages.gz
 3333 54180 main/binary-arm64/Packages
"""


def test_newest_version_is_chosen_numerically_not_lexically() -> None:
    assert build.select_entry(PACKAGES, None).version == "2.10.0"


def test_a_requested_version_is_chosen_with_its_own_hash() -> None:
    entry = build.select_entry(PACKAGES, "1.17180.0")
    assert (entry.sha256, entry.size) == ("aaaa", 10)


def test_a_version_absent_from_the_index_stops_the_build() -> None:
    with pytest.raises(SystemExit, match="3.0.0 is not in the repository index"):
        build.select_entry(PACKAGES, "3.0.0")


def test_other_packages_in_the_index_are_ignored() -> None:
    assert build.select_entry(PACKAGES, None).filename.startswith("pool/main/c/claude-desktop/")


def test_continuation_lines_stay_with_their_field() -> None:
    (first, *_) = build.parse_stanzas(PACKAGES)
    assert first["Depends"] == "libgtk-3-0,\nlibnss3"


def test_a_version_that_is_not_dotted_integers_is_refused() -> None:
    # dpkg would order 2.0~rc1 before 2.0; a tuple-of-ints sort cannot.
    with pytest.raises(ValueError, match="refusing to guess"):
        build.version_key("2.0~rc1")


def test_the_index_hash_comes_from_the_exact_path() -> None:
    assert build.index_sha256(RELEASE, "main/binary-arm64/Packages") == "3333"


def test_an_index_the_release_does_not_list_is_refused() -> None:
    with pytest.raises(build.VerificationError, match="no SHA256"):
        build.index_sha256(RELEASE, "main/binary-riscv64/Packages")


def test_a_tampered_download_is_refused() -> None:
    expected = hashlib.sha256(b"genuine").hexdigest()
    with pytest.raises(build.VerificationError, match="sha256"):
        build.check_sha256(b"tampered", expected, "the deb")


@pytest.mark.skipif(shutil.which("gpg") is None, reason="needs gpg")
def test_the_pinned_key_file_is_the_key_anthropic_publishes() -> None:
    shown = subprocess.run(
        ["gpg", "--show-keys", "--with-colons", str(build.KEYRING)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    fingerprints = [line.split(":")[9] for line in shown.splitlines() if line.startswith("fpr:")]
    assert fingerprints == [build.SIGNING_FINGERPRINT]


@pytest.mark.skipif(shutil.which("gpgv") is None, reason="needs gpgv")
def test_an_unsigned_inrelease_is_refused() -> None:
    with pytest.raises(build.VerificationError, match="not validly signed"):
        build.verify_inrelease(RELEASE.encode())
