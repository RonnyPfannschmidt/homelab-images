"""The tier that needs no cluster: does every chart lint, render and validate.

Seconds, and it catches the majority of what actually breaks a chart - a
template that does not parse, a value referenced under the wrong path, an
object that is not valid Kubernetes. The cluster tier exists for the things
this cannot see: admission, defaulting, and whether values from two sources
really merged.
"""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

from conftest import render, run

#: Objects whose schema kubeconform cannot know about, because they are not
#: upstream Kubernetes. Nothing here renders any today; the list exists so that
#: adding a CRD is a deliberate edit rather than a silent skip.
NON_UPSTREAM_KINDS: frozenset[str] = frozenset()


def test_chart_lints(chart: Path, ci_values: Path) -> None:
    run("helm", "lint", str(chart), "--values", str(ci_values))


def test_chart_renders(chart: Path, ci_values: Path) -> None:
    objects = render(chart, ci_values)
    assert objects, f"{chart.name} rendered nothing at all"


def test_no_workload_would_start_a_pod(chart: Path, ci_values: Path) -> None:
    """The lever the whole harness rests on, asserted rather than assumed.

    If a chart grows a Deployment whose replica count is a literal rather than
    a value, installing it in CI would pull a multi-gigabyte CUDA image onto a
    runner with 14 GB of disk. That failure is slow, confusing and expensive,
    so it is caught here where it costs a second.
    """
    for obj in render(chart, ci_values):
        if obj.get("kind") in {"Deployment", "StatefulSet", "ReplicaSet"}:
            replicas = obj.get("spec", {}).get("replicas")
            name = obj.get("metadata", {}).get("name")
            assert replicas == 0, (
                f"{chart.name}: {obj['kind']}/{name} renders replicas={replicas!r} "
                "under the CI overrides; its replica count is not driven by "
                "`.Values.workloads.<name>.replicaCount`"
            )


def test_every_object_is_namespaced_or_cluster_scoped_on_purpose(
    chart: Path, ci_values: Path
) -> None:
    """A chart owns one namespace; anything cluster-scoped is a surprise.

    Not a hard rule - a chart may legitimately need a ClusterRole one day - but
    it should be noticed when it starts to, because on a shared cluster that is
    the difference between an app and a privilege.
    """
    cluster_scoped = {"ClusterRole", "ClusterRoleBinding", "CustomResourceDefinition",
                      "PersistentVolume", "StorageClass", "Namespace"}
    found = [
        f"{o['kind']}/{o.get('metadata', {}).get('name')}"
        for o in render(chart, ci_values)
        if o.get("kind") in cluster_scoped
    ]
    assert not found, f"{chart.name} renders cluster-scoped objects: {found}"


@pytest.mark.skipif(shutil.which("kubeconform") is None, reason="kubeconform is not installed")
def test_rendered_objects_validate(chart: Path, ci_values: Path, tmp_path: Path) -> None:
    """Schema-validate against upstream Kubernetes, offline where possible."""
    rendered = tmp_path / "rendered.yaml"
    rendered.write_text(yaml.safe_dump_all(render(chart, ci_values)))
    run(
        "kubeconform",
        "-strict",
        "-summary",
        # A chart is written against whatever the cluster runs; pin it here so
        # the check means something specific.
        "-kubernetes-version", "1.32.0",
        *(arg for kind in NON_UPSTREAM_KINDS for arg in ("-skip", kind)),
        str(rendered),
    )


def test_chart_has_an_olares_manifest(chart: Path) -> None:
    """Transitional, and required only while the live box still runs Olares OS.

    The manifests are not what this repository is for - see README, "The Olares
    manifests are transitional". They are kept because `olares-apps
    helm-upgrade` cannot push a chart the Olares market will not validate, and
    the box has to stay serviceable until it is reinstalled onto k3s + Flux.

    This test exists to make the *sync* obligation mechanical: a chart edited
    without its manifest is what silently breaks the next install. It is
    deleted, along with the manifests, on the day the box is reinstalled.
    """
    manifest = chart / "OlaresManifest.yaml"
    assert manifest.is_file(), f"{chart.name} has no OlaresManifest.yaml"
    parsed = yaml.safe_load(manifest.read_text())
    assert parsed["metadata"]["name"] == chart.name, (
        f"{chart.name}: OlaresManifest metadata.name is "
        f"{parsed['metadata']['name']!r}; Olares resolves an app by directory name"
    )


def test_chart_version_matches_nothing_stale(chart: Path) -> None:
    """Chart.yaml and OlaresManifest.yaml each carry a version; neither may be absent.

    Transitional for the same reason as `test_chart_has_an_olares_manifest`.
    """
    chart_yaml = yaml.safe_load((chart / "Chart.yaml").read_text())
    manifest = yaml.safe_load((chart / "OlaresManifest.yaml").read_text())
    assert chart_yaml.get("version"), f"{chart.name}: Chart.yaml has no version"
    assert manifest["metadata"].get("version"), (
        f"{chart.name}: OlaresManifest.yaml has no metadata.version"
    )


def test_subchart_replica_count_matches_the_workload_lever(chart: Path) -> None:
    """A chart that imports a subchart has to keep two replica counts in step.

    Olares, the manifest's `workloadReplicas` and this harness all speak of a
    workload named after the chart. An imported subchart reads its own
    `replicaCount`, and Helm has no way to forward a parent value into a
    subchart from values.yaml - so the number is written twice, and the pair is
    pinned here rather than left to whoever edits one of them next.
    """
    chart_yaml = yaml.safe_load((chart / "Chart.yaml").read_text()) or {}
    dependencies = chart_yaml.get("dependencies") or []
    if not dependencies:
        pytest.skip(f"{chart.name} imports no subchart")

    values = yaml.safe_load((chart / "values.yaml").read_text()) or {}
    own = values.get("workloads", {}).get(chart.name, {}).get("replicaCount")
    assert own is not None, (
        f"{chart.name}: no workloads.{chart.name}.replicaCount to match against"
    )
    for dependency in dependencies:
        name = dependency["name"]
        theirs = (values.get(name) or {}).get("replicaCount")
        assert theirs == own, (
            f"{chart.name}: {name}.replicaCount is {theirs!r} but "
            f"workloads.{chart.name}.replicaCount is {own!r}; the subchart is "
            "what actually renders the Deployment, so these must agree"
        )


def test_vendored_dependencies_are_present_and_pinned(chart: Path) -> None:
    """An imported subchart has to be vendored, not fetched at install time.

    Olares installs an uploaded chart directory as-is and never resolves a
    `dependencies:` entry, so a chart whose workload lives in a subchart does
    not render at all unless the tarball is committed beside it.
    """
    chart_yaml = yaml.safe_load((chart / "Chart.yaml").read_text()) or {}
    for dependency in chart_yaml.get("dependencies") or []:
        vendored = chart / "charts" / f"{dependency['name']}-{dependency['version']}.tgz"
        assert vendored.is_file(), (
            f"{chart.name}: {vendored.name} is not vendored; run "
            f"`helm dependency update {chart}`"
        )


def test_packwiz_index_hash_is_current(chart: Path, ci_values: Path) -> None:
    """The pack's own index hash is what restarts the pod when mods change.

    The hand-written Deployment used a `checksum/packwiz` annotation over the
    rendered ConfigMap. The upstream subchart renders `podAnnotations` without
    `tpl`, so that is gone; the hash rides on the pack server's environment
    instead, where it does the same job. It is copied by hand out of pack.toml,
    which means it can go stale - so `packwiz refresh` without updating
    values.yaml fails here rather than silently shipping the old mods.
    """
    pack_toml = chart / "files" / "packwiz" / "pack.toml"
    if not pack_toml.is_file():
        pytest.skip(f"{chart.name} ships no packwiz pack")

    actual = tomllib.loads(pack_toml.read_text())["index"]["hash"]
    values = yaml.safe_load((chart / "values.yaml").read_text()) or {}
    declared = values.get("packwiz", {}).get("indexHash")
    assert declared == actual, (
        f"{chart.name}: values.yaml packwiz.indexHash is {declared!r} but "
        f"files/packwiz/pack.toml says {actual!r} - copy it across, or the pod "
        "will not restart onto the new pack"
    )

    rendered = yaml.safe_dump_all(render(chart, ci_values))
    assert actual in rendered, (
        f"{chart.name}: the pack index hash never reaches the rendered pod spec, "
        "so changing the pack would not restart the server"
    )


def test_packwiz_url_port_matches_the_pack_server(chart: Path) -> None:
    """PACKWIZ_URL and the pack server's httpd port are two literals, one port.

    They live in different halves of values.yaml - a subchart env var and a
    templated container - with no way to share a value between them. If they
    drift, itzg's /start aborts the boot on a connection refused.
    """
    values = yaml.safe_load((chart / "values.yaml").read_text()) or {}
    url = ((values.get("minecraft") or {}).get("extraEnv") or {}).get("PACKWIZ_URL")
    if not url:
        pytest.skip(f"{chart.name} serves no packwiz pack in-pod")

    port = urlparse(url).port
    init_containers = (values.get("minecraft") or {}).get("initContainers") or []
    serving = [
        yaml.safe_load(c) for c in init_containers if "httpd" in str(c)
    ]
    assert serving, f"{chart.name}: PACKWIZ_URL is set but nothing serves the pack"
    for container in serving:
        assert str(port) in [str(a) for a in container["command"]], (
            f"{chart.name}: PACKWIZ_URL points at port {port} but "
            f"{container['name']} serves on {container['command']}"
        )
