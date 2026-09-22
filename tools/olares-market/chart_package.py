"""`helm package`, without helm.

A chart package is a gzipped tar whose entries are `<chart name>/…`, and
nothing about producing one needs a 50 MB Go binary. Doing it here buys two
things the binary does not:

- **The market server runs where helm is not.** Uberspace is shared hosting;
  the service that rebuilds the catalog after a `git pull` is stdlib Python
  and stays that way.
- **Byte-reproducible output.** `helm package` stamps each entry with the
  file's mtime, so two packagings of one commit differ and a rebuild looks
  like a new chart to anything comparing bytes. Everything here is pinned.

What it deliberately does not reproduce is helm's *re-serialization*: helm
loads a chart into memory and writes it back out, which turns a vendored
`charts/<dep>-<version>.tgz` into an unpacked `charts/<dep>/…` tree. This
copies the directory as it stands. Both are valid packages - helm's own
loader accepts either, and `testing/test_market_build.py` asserts that ours
renders to the same Kubernetes objects as helm's.

The `.helmignore` semantics below are helm's, not git's, and the difference
bites: a pattern with no slash matches a *base name* anywhere in the tree, so
`*.tgz` in a chart that vendors a dependency would drop the dependency. That
is why `fichtendorf/.helmignore` anchors it as `/*.tgz`, and why the matcher
here refuses to treat `*` as crossing a separator.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import gzip
import io
import tarfile
from pathlib import Path, PurePosixPath

import yaml

#: Fixed metadata for every entry. The content is what identifies a chart;
#: mtime and ownership are noise that would otherwise make each rebuild a
#: different file. Mode is normalised for the same reason - a chart that was
#: checked out with a different umask must not package differently.
ENTRY_MTIME = 0
ENTRY_MODE = 0o644


def _matches(pattern: str, path: str) -> bool:
    """Go's `filepath.Match`, which is what helm's ignore rules are built on.

    The one behaviour worth naming: `*` does not cross a separator. Python's
    `fnmatch` has no such rule, so `*.tgz` would match
    `charts/minecraft-5.2.0.tgz` and silently drop a vendored dependency.
    Matching segment by segment restores it.
    """
    pattern_parts = pattern.split("/")
    path_parts = path.split("/")
    if len(pattern_parts) != len(path_parts):
        return False
    return all(
        fnmatch.fnmatchcase(part, glob)
        for glob, part in zip(pattern_parts, path_parts, strict=True)
    )


@dataclasses.dataclass(frozen=True)
class Rule:
    """One `.helmignore` line."""

    pattern: str
    #: A pattern written `foo/` matches directories only.
    directories_only: bool
    #: A pattern containing a slash is matched against the path relative to
    #: the chart root; one without is matched against the base name, anywhere
    #: in the tree. A leading slash anchors to the root and is stripped.
    anchored: bool

    @classmethod
    def parse(cls, line: str) -> Rule | None:
        pattern = line.rstrip()
        if not pattern or pattern.startswith("#"):
            return None
        if pattern.startswith("!"):
            # helm's own parser rejects these rather than ignoring them, and
            # silently including a file the author excluded is worse than a
            # loud failure.
            raise ValueError(f".helmignore: exclusion patterns are not supported: {line!r}")
        directories_only = pattern.endswith("/")
        pattern = pattern.rstrip("/")
        anchored = "/" in pattern
        return cls(pattern.lstrip("/"), directories_only, anchored)

    def ignores(self, relative: PurePosixPath, is_directory: bool) -> bool:
        if self.directories_only and not is_directory:
            return False
        return _matches(self.pattern, str(relative) if self.anchored else relative.name)


@dataclasses.dataclass(frozen=True)
class Ignore:
    """A chart's `.helmignore`, or an empty rule set when it has none."""

    rules: tuple[Rule, ...]

    @classmethod
    def read(cls, chart_directory: Path) -> Ignore:
        helmignore = chart_directory / ".helmignore"
        if not helmignore.is_file():
            return cls(())
        parsed = (Rule.parse(line) for line in helmignore.read_text().splitlines())
        return cls(tuple(rule for rule in parsed if rule is not None))

    def ignores(self, relative: PurePosixPath, is_directory: bool) -> bool:
        return any(rule.ignores(relative, is_directory) for rule in self.rules)


def chart_files(chart_directory: Path) -> list[Path]:
    """Every file that belongs in the package, in a stable order.

    A directory that matches is pruned rather than walked, which is helm's
    behaviour and also the only reading of `docker/` that makes sense.
    """
    ignore = Ignore.read(chart_directory)
    found: list[Path] = []

    def walk(directory: Path) -> None:
        for entry in sorted(directory.iterdir()):
            relative = PurePosixPath(entry.relative_to(chart_directory).as_posix())
            if ignore.ignores(relative, entry.is_dir()):
                continue
            if entry.is_dir():
                walk(entry)
            elif entry.is_file():
                found.append(entry)

    walk(chart_directory)
    return found


def chart_metadata(chart_directory: Path) -> tuple[str, str]:
    """The name and version `helm package` would put in the file name."""
    chart = yaml.safe_load((chart_directory / "Chart.yaml").read_text())
    return str(chart["name"]), str(chart["version"])


def package_chart(chart_directory: Path, destination: Path) -> Path:
    """Write `<destination>/<name>-<version>.tgz` and return its path.

    The top-level directory inside the archive is the chart's declared name,
    not the directory's - helm resolves a chart by what `Chart.yaml` says,
    and so does Olares.
    """
    name, version = chart_metadata(chart_directory)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"{name}-{version}.tgz"

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for path in chart_files(chart_directory):
            relative = path.relative_to(chart_directory).as_posix()
            info = tarfile.TarInfo(f"{name}/{relative}")
            payload = path.read_bytes()
            info.size = len(payload)
            info.mtime = ENTRY_MTIME
            info.mode = ENTRY_MODE
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(payload))

    # `mtime=0` and no embedded file name: the gzip header is otherwise two
    # more bytes that change per build.
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", filename="", mtime=0) as gz:
        gz.write(raw.getvalue())
    target.write_bytes(compressed.getvalue())
    return target
