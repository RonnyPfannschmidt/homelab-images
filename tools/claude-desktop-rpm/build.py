#!/usr/bin/env python3
"""Repackage Anthropic's official Claude Desktop .deb as an RPM, for local install.

Anthropic publishes Claude Desktop for Linux only as a Debian package in a
signed apt repository. This fetches the package from that repository, checks it
the way apt would, and runs `rpmbuild` over it in a Fedora container. The
result is for this machine: the app is proprietary and nothing here grants a
right to redistribute it, so the RPM is never published anywhere.

The chain of trust is apt's, end to end. `InRelease` must carry a good
signature from the pinned key; the `Packages` index must match the SHA256 that
the signed `InRelease` records for it; the .deb must match the SHA256 and size
that the index records for it. Only the text gpgv prints back as verified is
read, never the raw clearsigned file, so an unsigned section around the
signature cannot slip a different hash in.
"""

from __future__ import annotations

import argparse
import hashlib
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = "https://downloads.claude.ai/claude-desktop/apt/stable"
SUITE = "stable"
COMPONENT = "main"
PACKAGE = "claude-desktop"

#: Anthropic Claude Code Release Signing <security@anthropic.com>, as published
#: on https://code.claude.com/docs/en/desktop-linux. The .asc beside this file
#: is that key; it is checked against this fingerprint before every use.
SIGNING_FINGERPRINT = "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE"
KEYRING = HERE / "claude-desktop-archive-keyring.asc"
SPEC = HERE / "claude-desktop.spec"

#: rpm's architecture name for each Debian one the repository publishes.
DEB_ARCH = {"x86_64": "amd64", "aarch64": "arm64"}

DEFAULT_IMAGE = "registry.fedoraproject.org/fedora:44"


class VerificationError(Exception):
    """Something downloaded did not match what the signed index says it is."""


@dataclass(frozen=True)
class DebEntry:
    version: str
    filename: str
    sha256: str
    size: int


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        data: bytes = response.read()
    return data


def parse_stanzas(text: str) -> list[dict[str, str]]:
    """Split a deb822 file into stanzas; continuation lines join their field."""
    stanzas: list[dict[str, str]] = []
    current: dict[str, str] = {}
    field = ""
    for line in text.splitlines():
        if not line.strip():
            if current:
                stanzas.append(current)
            current, field = {}, ""
        elif line[0] in " \t":
            current[field] += "\n" + line.strip()
        else:
            field, _, value = line.partition(":")
            current[field] = value.strip()
    if current:
        stanzas.append(current)
    return stanzas


def version_key(version: str) -> tuple[int, ...]:
    """Order upstream versions; they have only ever been dotted integers.

    Anything else would need dpkg's full comparison algorithm, so it fails here
    rather than being sorted by a rule that is only nearly right.
    """
    parts = version.split(".")
    if not all(part.isdigit() for part in parts):
        raise ValueError(f"version {version!r} is not dotted integers; refusing to guess its order")
    return tuple(int(part) for part in parts)


def index_sha256(release: str, path: str) -> str:
    """The SHA256 a verified Release file records for one index path."""
    (stanza,) = parse_stanzas(release)
    for line in stanza.get("SHA256", "").splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[2] == path:
            return fields[0]
    raise VerificationError(f"the signed Release lists no SHA256 for {path}")


def select_entry(packages: str, version: str | None) -> DebEntry:
    """Pick the newest `claude-desktop`, or the one asked for, from a Packages index."""
    entries = [
        DebEntry(s["Version"], s["Filename"], s["SHA256"], int(s["Size"]))
        for s in parse_stanzas(packages)
        if s.get("Package") == PACKAGE
    ]
    if version is not None:
        entries = [e for e in entries if e.version == version]
        if not entries:
            raise SystemExit(f"{PACKAGE} {version} is not in the repository index")
    if not entries:
        raise SystemExit(f"the repository index lists no {PACKAGE}")
    return max(entries, key=lambda e: version_key(e.version))


def check_sha256(data: bytes, expected: str, what: str) -> None:
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise VerificationError(f"{what}: sha256 {actual}, the signed index says {expected}")


def verify_inrelease(inrelease: bytes) -> str:
    """Check the clearsigned InRelease against the pinned key; return the signed text."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        keyring = home / "keyring.gpg"
        # gpgv only reads binary keyrings; apt dearmors a signed-by .asc the same way.
        subprocess.run(
            ["gpg", "--homedir", str(home), "--dearmor", "--output", str(keyring), str(KEYRING)],
            check=True,
            capture_output=True,
        )
        result = subprocess.run(
            [
                "gpgv",
                "--homedir",
                str(home),
                "--keyring",
                str(keyring),
                "--status-fd",
                "2",
                "--output",
                "-",
                "-",
            ],
            input=inrelease,
            capture_output=True,
            check=False,  # a bad signature is reported below, with gpgv's status lines
        )
    status = result.stderr.decode(errors="replace").splitlines()
    signers = [line.split()[2] for line in status if line.startswith("[GNUPG:] VALIDSIG ")]
    if result.returncode != 0 or signers != [SIGNING_FINGERPRINT]:
        raise VerificationError(
            "InRelease is not validly signed by the pinned key:\n" + "\n".join(status)
        )
    return result.stdout.decode()


def resolve(arch: str, version: str | None) -> DebEntry:
    """The verified index entry for the package to build."""
    release = verify_inrelease(fetch(f"{REPO}/dists/{SUITE}/InRelease"))
    index_path = f"{COMPONENT}/binary-{DEB_ARCH[arch]}/Packages"
    packages = fetch(f"{REPO}/dists/{SUITE}/{index_path}")
    check_sha256(packages, index_sha256(release, index_path), index_path)
    return select_entry(packages.decode(), version)


def download_deb(entry: DebEntry, cache: Path) -> Path:
    target = cache / Path(entry.filename).name
    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == entry.sha256:
        return target
    print(f"downloading {entry.filename} ({entry.size / 2**20:.0f} MiB)", file=sys.stderr)
    data = fetch(f"{REPO}/{entry.filename}")
    if len(data) != entry.size:
        raise VerificationError(f"{entry.filename}: {len(data)} bytes, the index says {entry.size}")
    check_sha256(data, entry.sha256, entry.filename)
    target.write_bytes(data)
    return target


def rpmbuild(entry: DebEntry, deb: Path, arch: str, out: Path, image: str) -> None:
    """Run rpmbuild in a throwaway Fedora container and leave the RPM in `out`."""
    with tempfile.TemporaryDirectory(dir=out, prefix=".rpmbuild-") as tmp:
        topdir = Path(tmp)
        (topdir / "SOURCES").mkdir()
        shutil.copy2(deb, topdir / "SOURCES" / deb.name)
        shutil.copy2(SPEC, topdir / SPEC.name)
        # Rootless podman maps the container's root to the invoking user, so the
        # result is already owned by them. A chown inside would hand it to a
        # subordinate uid that the caller cannot even delete.
        script = (
            "dnf -y -q install rpm-build binutils tar xz >/dev/null"
            f" && rpmbuild -bb --target {arch}"
            " --define '_topdir /work'"
            f" --define 'upstream_version {entry.version}'"
            f" /work/{SPEC.name}"
        )
        subprocess.run(
            [
                "podman",
                "run",
                "--rm",
                "--pull=missing",
                "--volume",
                f"{topdir}:/work:Z",
                image,
                "sh",
                "-c",
                script,
            ],
            check=True,
        )
        for rpm in (topdir / "RPMS").rglob("*.rpm"):
            shutil.move(rpm, out / rpm.name)
            print(out / rpm.name)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        epilog="examples:\n"
        "  build.py                          newest version, this machine's architecture\n"
        "  build.py --version 2.26454.2      a specific upstream version\n"
        "  build.py --arch aarch64           for the Pi, built on this machine\n"
        "  build.py --check                  print the newest version and stop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    host_arch = platform.machine()
    parser.add_argument(
        "--arch",
        choices=sorted(DEB_ARCH),
        default=host_arch,
        help="target architecture (default: this machine's)",
    )
    parser.add_argument("--version", help="upstream version to build (default: newest)")
    parser.add_argument(
        "--out",
        type=Path,
        default=HERE / "dist",
        help="where the .deb is cached and the RPM is written (default: %(default)s)",
    )
    parser.add_argument(
        "--image",
        default=DEFAULT_IMAGE,
        help="Fedora image rpmbuild runs in (default: %(default)s)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the index and print the version that would be built",
    )
    args = parser.parse_args()
    if args.arch not in DEB_ARCH:
        parser.error(f"Anthropic publishes no package for {args.arch}")

    out: Path = args.out.resolve()
    entry = resolve(args.arch, args.version)
    if args.check:
        print(entry.version)
        return
    out.mkdir(parents=True, exist_ok=True)
    rpmbuild(entry, download_deb(entry, out), args.arch, out, args.image)


if __name__ == "__main__":
    main()
