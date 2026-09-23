"""What the generated market source has to be true of, per build.

The protocol these assertions encode is not documented upstream - it was read
off a working third-party source and is restated in
`tools/olares-market/build_market.py`. That makes it exactly the kind of thing
a test should hold: nothing in a chart edit would otherwise notice if a field
the Go syncer needs stopped being written, and the symptom on the box is a
source that syncs to zero apps and reads like an auth failure.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT

TOOL = REPO_ROOT / "tools" / "olares-market" / "build_market.py"


def load_tool(name: str) -> Any:
    """Import one of the tool's modules by path; its directory is not a package."""
    if str(TOOL.parent) not in sys.path:
        # build_market imports chart_package as a sibling, which only works
        # with its directory on the path - as it is when either is run as a
        # script.
        sys.path.insert(0, str(TOOL.parent))
    spec = importlib.util.spec_from_file_location(name, TOOL.parent / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def build_market() -> Any:
    return load_tool("build_market")


@pytest.fixture(scope="module")
def chart_package() -> Any:
    return load_tool("chart_package")


@pytest.fixture(scope="module")
def site(build_market: Any, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One real build, shared between the assertions below.

    Needs no helm: packaging is `chart_package`, which is the property that
    lets the market service rebuild itself on a host that has no helm.
    """
    out = tmp_path_factory.mktemp("site") / "site"
    assert build_market.main(["--out", str(out)]) == 0
    return out


def read(site: Path, *parts: str) -> Any:
    return json.loads(site.joinpath(*parts).read_text())


def test_every_chart_reaches_the_catalog(site: Path) -> None:
    charts = {p.name for p in (REPO_ROOT / "olares-apps").iterdir() if (p / "Chart.yaml").is_file()}
    catalog = read(site, "catalog.json")
    assert {app["name"] for app in catalog["summaries"].values()} == charts


def test_the_landing_page_names_the_live_source_url(site: Path) -> None:
    """The service runs the build with no URL flags, so the default is live.

    It used to be a GitHub Pages address from a design that was never
    deployed, and the page told readers to add a source that did not exist.
    """
    landing = (site / "index.html").read_text()
    assert "<code>https://olares-market.ronnypfannschmidt.de</code>" in landing
    assert "github.io" not in landing


def test_the_syncer_can_enumerate_the_apps(site: Path) -> None:
    """`tops` is what the Go parser iterates; the flat dict alone yields zero.

    A catalog missing it is not an error anywhere - it is an empty source.
    """
    info = read(site, "api", "v1", "appstore", "info")
    tops = info["data"]["tops"]
    assert [entry["rank"] for entry in tops] == list(range(1, len(info["data"]["apps"]) + 1))
    assert {entry["appId"] for entry in tops} == {
        app["name"] for app in info["data"]["apps"].values()
    }


def test_every_timestamp_has_nine_digits_of_fraction(site: Path) -> None:
    """Three-digit milliseconds do not fail to parse loudly; the app is skipped."""
    stamps: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"updated_at", "last_updated", "createdAt", "mergedAt"} and isinstance(
                    value, str
                ):
                    stamps.append(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for name in ("catalog.json", "api/v1/appstore/hash", "api/v1/appstore/info"):
        walk(read(site, *name.split("/")))

    assert stamps
    for stamp in stamps:
        assert len(stamp.split(".")[1]) == 10, f"{stamp} is not nanosecond-precision"
        assert stamp.endswith("Z")


def test_every_category_is_in_the_sidebar(site: Path) -> None:
    """A category the Market has no tag for is a category nothing can be found under."""
    info = read(site, "api", "v1", "appstore", "info")
    used = {category for app in info["data"]["apps"].values() for category in app["categories"]}
    assert used <= set(info["data"]["tags"])


def test_each_app_has_a_chart_at_the_url_olares_asks_for(site: Path) -> None:
    for app in read(site, "catalog.json")["summaries"].values():
        served = site / "api" / "v1" / "applications" / app["name"] / "chart"
        assert served.is_file(), f"{app['name']} has no chart at its API path"
        with tarfile.open(served) as tar:
            names = tar.getnames()
        assert f"{app['name']}/Chart.yaml" in names


def test_the_advertised_chart_file_is_the_one_published(site: Path) -> None:
    """`chartName` is what the installer asks for by `?fileName=`."""
    for app_id, detail in read(site, "catalog.json")["details"].items():
        assert (site / "charts" / detail["chartName"]).is_file(), (
            f"{app_id} advertises {detail['chartName']}, which is not in charts/"
        )


def test_the_detail_payload_covers_every_summary(site: Path) -> None:
    """The POST endpoint answers by id; a summary with no detail is a dead app page."""
    catalog = read(site, "catalog.json")
    served = read(site, "api", "v1", "applications", "info")
    assert set(catalog["details"]) == set(catalog["summaries"])
    assert set(served["apps"]) == set(catalog["summaries"])


def test_the_build_is_reproducible(build_market: Any, site: Path, tmp_path: Path) -> None:
    """Two builds of one commit must be byte-identical.

    Not tidiness: `updated_at` is published, so a clock-derived value would
    make every CI run a content change - a Pages deployment per run, and a
    catalog hash that moves, which is the signal Olares re-syncs on.
    """
    again = tmp_path / "again"
    assert build_market.main(["--out", str(again), "--skip-charts"]) == 0
    for name in ("catalog.json", "api/v1/appstore/hash", "api/v1/appstore/info"):
        assert read(site, *name.split("/")) == read(again, *name.split("/")), (
            f"{name} differs between two builds of the same tree"
        )


def test_app_ids_are_stable_against_the_documented_derivation(build_market: Any) -> None:
    """Olares stores this id against an install; a changed derivation orphans it."""
    assert build_market.app_id("qwen38hyperqwen") == "a85ac1d4"
    assert build_market.app_id("fichtendorf") == "b046d2d6"


# ---------------------------------------------------------------------------
# packaging without helm
#
# `chart_package` replaces `helm package` so that the market service can
# rebuild itself on a host with no helm binary. That is only safe while the
# two agree, and "agree" has to be defined: not byte-identical, because helm
# re-serialises what it loads - it rewrites Chart.yaml (dropping quotes,
# rewrapping long lines) and unpacks a vendored `charts/<dep>.tgz` into a
# directory. Both forms are valid packages. What must not differ is the set
# of files and what helm renders from them.
# ---------------------------------------------------------------------------

CHARTS = sorted(p for p in (REPO_ROOT / "olares-apps").iterdir() if (p / "Chart.yaml").is_file())


@pytest.fixture
def helm_required() -> None:
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")


def members(archive: Path) -> dict[str, bytes]:
    with tarfile.open(archive) as tar:
        return {
            member.name: (tar.extractfile(member) or io_error(member)).read()
            for member in tar.getmembers()
            if member.isfile()
        }


def io_error(member: tarfile.TarInfo) -> Any:
    raise AssertionError(f"{member.name} could not be read out of the archive")


def vendored_form(names: set[str]) -> set[str]:
    """Collapse helm's unpacked subchart back to the tarball it came from.

    `helm package` loads a chart and writes it out again, so a committed
    `charts/minecraft-5.2.0.tgz` comes back as `charts/minecraft/…`. Ours
    copies the file. Comparing the two needs one of them normalised, and the
    dependency's *name* is the part that carries meaning either way.
    """
    collapsed = set()
    for name in names:
        parts = name.split("/")
        if len(parts) > 2 and parts[1] == "charts":
            collapsed.add(f"{parts[0]}/charts/{parts[2].split('-')[0]}")
        else:
            collapsed.add(name)
    return collapsed


@pytest.mark.parametrize("chart_dir", CHARTS, ids=[c.name for c in CHARTS])
def test_the_package_holds_what_helm_would_package(
    chart_dir: Path, chart_package: Any, helm_required: None, tmp_path: Path
) -> None:
    ours = chart_package.package_chart(chart_dir, tmp_path / "ours")
    subprocess.run(
        ["helm", "package", str(chart_dir), "--destination", str(tmp_path / "helm")],
        check=True,
        capture_output=True,
    )
    theirs = tmp_path / "helm" / ours.name

    assert vendored_form(set(members(ours))) == vendored_form(set(members(theirs)))


@pytest.mark.parametrize("chart_dir", CHARTS, ids=[c.name for c in CHARTS])
def test_the_package_renders_to_what_helms_does(
    chart_dir: Path, chart_package: Any, helm_required: None, tmp_path: Path
) -> None:
    """The assertion that actually matters: same chart in, same objects out."""
    ours = chart_package.package_chart(chart_dir, tmp_path / "ours")
    subprocess.run(
        ["helm", "package", str(chart_dir), "--destination", str(tmp_path / "helm")],
        check=True,
        capture_output=True,
    )

    def rendered(package: Path) -> str:
        result = subprocess.run(
            ["helm", "template", "t", str(package)], check=True, capture_output=True, text=True
        )
        return result.stdout

    assert rendered(ours) == rendered(tmp_path / "helm" / ours.name)


@pytest.mark.parametrize("chart_dir", CHARTS, ids=[c.name for c in CHARTS])
def test_packaging_is_byte_reproducible(
    chart_dir: Path, chart_package: Any, tmp_path: Path
) -> None:
    """The reason for pinning mtime, mode and ownership, and the gzip header.

    `helm package` stamps each entry with the file's mtime, so its output
    differs per checkout. Ours must not: the market publishes these, and a
    tarball that changes without its chart changing is a chart nobody can
    verify.
    """
    first = (chart_package.package_chart(chart_dir, tmp_path / "a")).read_bytes()
    second = (chart_package.package_chart(chart_dir, tmp_path / "b")).read_bytes()
    assert first == second
    # Decompressed too, in case a gzip header field ever starts carrying a
    # timestamp again - that would be invisible in the comparison above only
    # if both runs happened in the same second.
    assert gzip.decompress(first) == gzip.decompress(second)


def test_a_vendored_dependency_survives_packaging(chart_package: Any, tmp_path: Path) -> None:
    """`*.tgz` in a .helmignore is the trap this repository already walked into.

    Olares never resolves a `dependencies:` entry - it installs what is in
    the tarball - so a chart whose vendored subchart was ignored renders
    nothing. fichtendorf's .helmignore anchors the pattern as `/*.tgz` for
    exactly this reason, and that only works if the matcher refuses to let
    `*` cross a directory separator.
    """
    packaged = chart_package.package_chart(REPO_ROOT / "olares-apps" / "fichtendorf", tmp_path)
    assert any(name.startswith("fichtendorf/charts/") for name in members(packaged))


def test_helmignore_rules_are_helms_not_gits(chart_package: Any, tmp_path: Path) -> None:
    """Base-name matching, anchoring and directory-only patterns, in one place."""
    chart = tmp_path / "demo"
    (chart / "templates").mkdir(parents=True)
    (chart / "charts").mkdir()
    (chart / "docker").mkdir()
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: demo\nversion: 1.0.0\n")
    (chart / "values.yaml").write_text("{}\n")
    (chart / "templates" / "deployment.yaml").write_text("{}\n")
    (chart / "docker" / "Containerfile").write_text("FROM scratch\n")
    (chart / "demo-1.0.0.tgz").write_bytes(b"stale build artifact")
    (chart / "charts" / "dep-1.0.0.tgz").write_bytes(b"vendored dependency")
    (chart / ".helmignore").write_text("# comment\n\ndocker/\n/*.tgz\n")

    built = chart_package.package_chart(chart, tmp_path / "out")
    packaged = {name.removeprefix("demo/") for name in members(built)}

    assert "charts/dep-1.0.0.tgz" in packaged, "an anchored pattern must not match a subdirectory"
    assert "demo-1.0.0.tgz" not in packaged, "the anchored pattern must match at the root"
    assert not any(name.startswith("docker/") for name in packaged), (
        "a matching directory is pruned"
    )
    assert ".helmignore" in packaged, "helm ships the ignore file itself; so do we"


def test_an_exclusion_pattern_is_refused_rather_than_ignored(chart_package: Any) -> None:
    """helm does not support `!`, and silently including the file is worse."""
    with pytest.raises(ValueError, match="exclusion patterns"):
        chart_package.Rule.parse("!keep-me.yaml")
