"""The tier that needs a cluster: a mini version of the real deployment.

Two things only a running API server can answer. First, whether a chart's
objects are *accepted* - defaulting, validation and admission are not visible
to `helm template`. Second, and the reason this file exists at all, whether the
public/private split actually works: a chart pulled from this repository by
Flux, joined at install time to values that arrive from a Secret standing in
for the private side.

That second test is the design under test, not the charts. If
`HelmRelease.spec.valuesFrom` ever stops merging the way this assumes, every
app in the cluster gets its defaults instead of its configuration, and the
symptom is a working deployment with the wrong settings - the kind of failure
that is found weeks later.

    KIND_TESTS=1 uv run pytest testing/test_kind_deploy.py -v
    KIND_TESTS=1 FLUX_TESTS=1 uv run pytest testing/test_kind_deploy.py -v

The Flux tier needs the commit under test to be on `origin`, because Flux
fetches this repository over the network like any other consumer - which is
also the point: it needs no credential to do it.
"""

from __future__ import annotations

import base64
import os
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

from conftest import FLUX_VERSION, REPO_ROOT, kube_json, kubectl, run

#: One chart, not all four, for the Flux rehearsal. What is being tested is the
#: wiring between the two repositories; a second chart exercises the same path
#: and doubles the wall clock. fichtendorf is the one with no GPU request and
#: the smallest object set.
FLUX_CHART = "fichtendorf"

PUBLIC_REPO_URL = "https://github.com/RonnyPfannschmidt/homelab-images"


def test_chart_installs(chart: Path, ci_values: Path, kube: str) -> None:
    """Install for real, then read the objects back out of the API.

    `--wait=false` with every workload at zero replicas: helm applies the
    objects and returns, no pod is ever scheduled, and no image is pulled.
    """
    namespace = f"ci-{chart.name}"
    kubectl(kube, "create", "namespace", namespace, check=False)
    try:
        run(
            "helm", "install", chart.name, str(chart),
            "--kubeconfig", kube,
            "--namespace", namespace,
            "--values", str(ci_values),
            "--wait=false",
            timeout=300,
        )
        deployments = kube_json(kube, "get", "deployments", "-n", namespace)
        assert deployments["items"], f"{chart.name} installed but created no Deployment"
        for item in deployments["items"]:
            name = item["metadata"]["name"]
            assert item["spec"]["replicas"] == 0, (
                f"{chart.name}: the API server has {name} at "
                f"{item['spec']['replicas']} replicas; it would pull images"
            )
    finally:
        run("helm", "uninstall", chart.name, "--kubeconfig", kube,
            "--namespace", namespace, check=False)
        kubectl(kube, "delete", "namespace", namespace, "--wait=false", check=False)


# --------------------------------------------------------------------------
# the Flux rehearsal


def _flux_unavailable() -> str | None:
    if not os.environ.get("FLUX_TESTS"):
        return "set FLUX_TESTS=1 to run; this installs Flux and fetches over the network"
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True
    ).stdout.strip()
    on_remote = subprocess.run(
        ["git", "branch", "-r", "--contains", head],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if not on_remote.stdout.strip():
        return f"commit {head[:8]} is not on any remote; Flux fetches from {PUBLIC_REPO_URL}"
    return None


@pytest.fixture(scope="session")
def flux(kube: str) -> str:
    """Flux installed into the kind cluster, from its pinned release manifest.

    `kubectl apply` of the release rather than the `flux` CLI: one fewer tool
    to install on a runner, and the manifest is what `flux install` applies
    anyway.
    """
    if (why := _flux_unavailable()) is not None:
        pytest.skip(why)
    url = f"https://github.com/fluxcd/flux2/releases/download/{FLUX_VERSION}/install.yaml"
    kubectl(kube, "apply", "-f", url, timeout=600)
    kubectl(
        kube, "wait", "--for=condition=Available",
        "deployment", "--all", "-n", "flux-system", "--timeout=300s",
        timeout=360,
    )
    return kube


def test_flux_joins_a_public_chart_to_private_values(flux: str, tmp_path: Path) -> None:
    """The split, end to end, in a throwaway cluster.

    Flux fetches this repository - anonymously, over HTTPS, with no `secretRef`
    on the GitRepository, which is the property that makes the public half
    worth having. The values arrive separately, from a Secret that stands in
    for what a sops-decrypted private Kustomization would create. The assertion
    is that the workload in the cluster carries a setting that exists in
    neither the chart's defaults nor the repository: only in the Secret.
    """
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True
    ).stdout.strip()
    namespace = "ci-flux"

    # What the private side would hand over: the CI lever, plus one marker that
    # cannot come from anywhere else, so a merge that silently did not happen
    # is distinguishable from one that did.
    marker = "joined-from-the-private-side"
    private_values = yaml.safe_dump(
        {
            "workloads": {"fichtendorf": {"replicaCount": 0}, "terminal": {"replicaCount": 0}},
            "userspace": {"appData": "/tmp/appdata"},
            "server": {"motd": marker},
        }
    )

    manifests = textwrap.dedent(f"""\
        apiVersion: v1
        kind: Namespace
        metadata:
          name: {namespace}
        ---
        apiVersion: v1
        kind: Secret
        metadata:
          name: private-values
          namespace: {namespace}
        data:
          values.yaml: {base64.b64encode(private_values.encode()).decode()}
        ---
        apiVersion: source.toolkit.fluxcd.io/v1
        kind: GitRepository
        metadata:
          name: homelab-images
          namespace: flux-system
        spec:
          interval: 1m
          url: {PUBLIC_REPO_URL}
          # Pinned to the commit under test rather than a branch: a branch
          # would test whatever main happened to be when the job ran.
          ref:
            commit: {head}
          # No secretRef. A public repository needs none, and that is the whole
          # argument for this repository being public.
        ---
        apiVersion: helm.toolkit.fluxcd.io/v2
        kind: HelmRelease
        metadata:
          name: {FLUX_CHART}
          namespace: {namespace}
        spec:
          interval: 5m
          chart:
            spec:
              chart: olares-apps/{FLUX_CHART}
              sourceRef:
                kind: GitRepository
                name: homelab-images
                namespace: flux-system
          # The objects have to land; the pods do not. Every workload is at zero
          # replicas, so waiting would wait for nothing and time out.
          install:
            disableWait: true
          upgrade:
            disableWait: true
          valuesFrom:
            - kind: Secret
              name: private-values
              valuesKey: values.yaml
        """)
    path = tmp_path / "flux.yaml"
    path.write_text(manifests)

    kubectl(flux, "apply", "-f", str(path))
    kubectl(
        flux, "wait", "--for=condition=Ready",
        f"helmrelease/{FLUX_CHART}", "-n", namespace, "--timeout=300s",
        timeout=360,
    )

    deployment = kube_json(flux, "get", "deployment", FLUX_CHART, "-n", namespace)
    assert deployment["spec"]["replicas"] == 0

    rendered = yaml.safe_dump(deployment)
    assert marker in rendered, (
        "the workload does not carry the marker from the Secret: the chart came "
        "from git but the values did not merge, which is the failure this test "
        "exists for"
    )
