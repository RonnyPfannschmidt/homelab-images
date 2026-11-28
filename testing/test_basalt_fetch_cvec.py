"""The control-vector init container of `basaltqwen38abl`, run for real.

1.3.3 kept a matching file with ``raise SystemExit(f"{dest} present")``, which
exits 1: the first install downloaded the vector and passed, and every start
after it crash-looped in ``fetch-cvec``. Rendering cannot see that, so the
script is pulled out of the rendered Deployment and executed against a file
that is already in place.

Deliberately not using the ``chart`` / ``ci_values`` fixtures, as in
`test_hyperqwen_profiles.py`: this is about one chart.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import REPO_ROOT, render

CHART = REPO_ROOT / "olares-apps" / "basaltqwen38abl"


@pytest.fixture
def values(tmp_path: Path) -> Path:
    chart_values = yaml.safe_load((CHART / "values.yaml").read_text())
    path = tmp_path / "ci-values.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "workloads": {name: {"replicaCount": 0} for name in chart_values["workloads"]},
                "userspace": {"appData": "/tmp/appdata"},
            }
        )
    )
    return path


def fetch_cvec_script(values: Path) -> str:
    for obj in render(CHART, values):
        if obj.get("kind") != "Deployment":
            continue
        for container in obj["spec"]["template"]["spec"].get("initContainers") or ():
            if container["name"] == "fetch-cvec":
                command = container["command"]
                assert command[-2] == "-c", command
                return str(command[-1])
    raise AssertionError("no fetch-cvec init container rendered")


def test_fetch_cvec_keeps_a_matching_file_and_exits_zero(values: Path, tmp_path: Path) -> None:
    script = fetch_cvec_script(values)
    content = b"stand-in vector"
    cvec = tmp_path / "cvec"
    cvec.mkdir()
    real_sha = next(line.split('"')[1] for line in script.splitlines() if line.startswith("sha = "))
    (cvec / "Qwen3.8-Flash-Next-refusal-projection.gguf").write_bytes(content)
    script = script.replace('"/data/cvec"', repr(str(cvec))).replace(
        real_sha, hashlib.sha256(content).hexdigest()
    )

    # An endpoint nothing listens on: reaching for the network fails the test
    # instead of downloading the vector.
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        env={"HF_ENDPOINT": "http://127.0.0.1:9"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "present" in result.stdout
