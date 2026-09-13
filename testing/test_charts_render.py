"""The tier that needs no cluster: does every chart lint, render and validate.

Seconds, and it catches the majority of what actually breaks a chart - a
template that does not parse, a value referenced under the wrong path, an
object that is not valid Kubernetes. The cluster tier exists for the things
this cannot see: admission, defaulting, and whether values from two sources
really merged.
"""

from __future__ import annotations

import shutil
from pathlib import Path

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
    """An Olares app owns one namespace; anything cluster-scoped is a surprise.

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
    """Every chart here is also an Olares app; the manifest is not optional."""
    manifest = chart / "OlaresManifest.yaml"
    assert manifest.is_file(), f"{chart.name} has no OlaresManifest.yaml"
    parsed = yaml.safe_load(manifest.read_text())
    assert parsed["metadata"]["name"] == chart.name, (
        f"{chart.name}: OlaresManifest metadata.name is "
        f"{parsed['metadata']['name']!r}; Olares resolves an app by directory name"
    )


def test_chart_version_matches_nothing_stale(chart: Path) -> None:
    """Chart.yaml and OlaresManifest.yaml each carry a version; neither may be absent."""
    chart_yaml = yaml.safe_load((chart / "Chart.yaml").read_text())
    manifest = yaml.safe_load((chart / "OlaresManifest.yaml").read_text())
    assert chart_yaml.get("version"), f"{chart.name}: Chart.yaml has no version"
    assert manifest["metadata"].get("version"), (
        f"{chart.name}: OlaresManifest.yaml has no metadata.version"
    )
