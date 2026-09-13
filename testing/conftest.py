"""Shared fixtures: the charts, the values that make them safe to install, and
a throwaway cluster to install them into.

The whole harness turns on one lever. Every chart here drives its Deployment
replica count from ``.Values.workloads.<name>.replicaCount``, so setting all of
them to zero produces a release that applies every object and starts no pod -
which means no image pull, no GPU request that nothing can satisfy, and no
20 GB model download in a CI job whose disk is 14 GB. What is under test is the
wiring, not whether a model server boots; the model server needs the actual
hardware and is tested there.

That lever is generated from each chart's own ``values.yaml`` rather than
written out per chart, so a workload added later cannot quietly escape it.
``test_charts_render`` asserts the result really is zero replicas everywhere,
because if the lever ever stops working the failure mode is a CI job that
silently tries to pull a CUDA image.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parents[1]
CHARTS = sorted(p for p in (REPO_ROOT / "olares-apps").iterdir() if (p / "Chart.yaml").is_file())

#: Pinned rather than floating: a CI run that fails should fail because this
#: repository changed, not because an upstream published a release overnight.
FLUX_VERSION = "v2.9.5"


def run(*command: str, timeout: int = 600, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        tail = "\n".join((result.stdout + result.stderr).splitlines()[-40:])
        raise AssertionError(f"failed: {' '.join(command)}\n{tail}")
    return result


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrise over the charts by directory name, so failures name one."""
    if "chart" in metafunc.fixturenames:
        metafunc.parametrize("chart", CHARTS, ids=[c.name for c in CHARTS])


@pytest.fixture(scope="session", autouse=True)
def _helm_is_installed() -> None:
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed", allow_module_level=True)


@pytest.fixture
def ci_values(chart: Path, tmp_path: Path) -> Path:
    """Overrides that make `chart` safe to install: every workload at zero.

    Read out of the chart's own values rather than declared here. A chart that
    grows a second Deployment gets it covered without anybody remembering to.
    """
    values = yaml.safe_load((chart / "values.yaml").read_text()) or {}
    workloads = values.get("workloads") or {}
    assert workloads, (
        f"{chart.name} declares no workloads: this harness has no way to stop it "
        "starting pods, so it must not be installed"
    )

    override = {
        "workloads": {name: {"replicaCount": 0} for name in workloads},
        # Olares injects this at install time; the charts default it to "" so
        # that `helm template` works, but an empty hostPath renders as a
        # relative path in some templates. Give it a real absolute one.
        "userspace": {"appData": "/tmp/appdata"},
    }

    # Anything a single chart needs beyond the generated lever.
    extra = Path(__file__).parent / "values" / f"{chart.name}.yaml"
    if extra.is_file():
        override.update(yaml.safe_load(extra.read_text()) or {})

    path = tmp_path / "ci-values.yaml"
    path.write_text(yaml.safe_dump(override))
    return path


def render(chart: Path, values: Path, namespace: str = "ci") -> list[dict]:
    """`helm template`, parsed. Empty documents dropped."""
    out = run(
        "helm", "template", chart.name, str(chart),
        "--values", str(values), "--namespace", namespace,
    ).stdout
    return [doc for doc in yaml.safe_load_all(out) if doc]


# --------------------------------------------------------------------------
# the kind tier


def _kind_unavailable() -> str | None:
    if not os.environ.get("KIND_TESTS"):
        return "set KIND_TESTS=1 to run; this creates a cluster"
    for tool in ("kind", "kubectl"):
        if shutil.which(tool) is None:
            return f"{tool} is not installed"
    return None


@pytest.fixture(scope="session")
def kube() -> Iterator[str]:
    """A throwaway kind cluster, torn down however the session ends.

    Session-scoped: creating one costs about a minute, and every test here
    installs into its own namespace, so they do not need one each.
    """
    if (why := _kind_unavailable()) is not None:
        pytest.skip(why)

    name = "homelab-ci"
    kubeconfig = str(Path.home() / ".kube" / f"kind-{name}")
    run("kind", "create", "cluster", "--name", name, "--kubeconfig", kubeconfig, timeout=900)
    try:
        yield kubeconfig
    finally:
        run("kind", "delete", "cluster", "--name", name, check=False)


def kubectl(kubeconfig: str, *args: str, check: bool = True, **kw) -> subprocess.CompletedProcess[str]:
    return run("kubectl", "--kubeconfig", kubeconfig, *args, check=check, **kw)


def kube_json(kubeconfig: str, *args: str) -> dict:
    return json.loads(kubectl(kubeconfig, *args, "-o", "json").stdout)
