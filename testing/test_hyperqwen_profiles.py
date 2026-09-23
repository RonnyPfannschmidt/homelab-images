"""The one chart whose environment is computed rather than written out.

`qwen38hyperqwen` turns a profile *name* - picked in Olares Settings, or from
`profile.serving` when Settings leaves it empty - into the three variables the
engine's launcher actually reads. The mapping is data in `values.yaml`, so
what needs asserting is that the name reaches the ConfigMap, that the three
move together, and that a name nobody defined stops the render instead of
booting the engine on a silent default.

Deliberately not using the ``chart`` / ``ci_values`` fixtures: those are
parametrised over every chart in the repository, and these assertions are
about one of them. The values file here does the same job - every workload at
zero, so nothing in this file can start a pod.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import REPO_ROOT, run

CHART = REPO_ROOT / "olares-apps" / "qwen38hyperqwen"

#: What each profile must put in the engine's ConfigMap. Written out here
#: rather than read from values.yaml on purpose: a test that derives its
#: expectation from the file under test only proves the template can read
#: YAML. These three rows are the upstream-vetted combinations, and changing
#: one should have to be deliberate in two places.
EXPECTED = {
    "fast": {"CTX": "fast", "SPEC": "dflash2", "PREFIX_CACHE": "1"},
    "long": {"CTX": "long", "SPEC": "mtp", "PREFIX_CACHE": "1"},
    "huge": {"CTX": "huge", "SPEC": "mtp", "PREFIX_CACHE": "0"},
}


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


def template(values: Path, *overrides: str, check: bool = True) -> str:
    result = run(
        "helm",
        "template",
        "t",
        str(CHART),
        "--values",
        str(values),
        *overrides,
        check=check,
    )
    return result.stdout + result.stderr


def engine_env(values: Path, *overrides: str) -> dict[str, str]:
    for obj in yaml.safe_load_all(template(values, *overrides)):
        if obj and obj.get("kind") == "ConfigMap" and obj["metadata"]["name"] == "hyperqwen-env":
            data: dict[str, str] = obj["data"]
            return data
    raise AssertionError("the chart rendered no hyperqwen-env ConfigMap")


@pytest.mark.parametrize("profile", sorted(EXPECTED))
def test_settings_profile_drives_all_three_axes(profile: str, values: Path) -> None:
    """SERVING_PROFILE is what Olares Settings writes; it must reach the engine."""
    data = engine_env(values, "--set", f"olaresEnv.SERVING_PROFILE={profile}")
    for key, expected in EXPECTED[profile].items():
        assert data[key] == expected, f"profile {profile}: {key} is {data[key]!r}"


def test_an_empty_settings_value_falls_back_to_the_chart_default(values: Path) -> None:
    """Olares renders an unset optional env as "", not as absent.

    That is the whole reason the template reads it with `default` rather than
    `with`: an app whose owner never touched the Settings field would
    otherwise render an empty CTX and boot on whatever the launcher guesses.
    """
    assert engine_env(values, "--set", "olaresEnv.SERVING_PROFILE=")["CTX"] == "fast"
    assert engine_env(values)["CTX"] == "fast"


def test_an_unknown_profile_fails_the_render(values: Path) -> None:
    """A typo must not reach the engine, where it costs a crashloop to find."""
    output = template(values, "--set", "olaresEnv.SERVING_PROFILE=fastest", check=False)
    assert "is not one of: fast, huge, long" in output


def test_extra_args_is_omitted_when_nobody_set_it(values: Path) -> None:
    """A ConfigMap key with an empty value is a variable that is SET.

    The launcher reads EXTRA_ARGS with the bare-hyphen form, so `EXTRA_ARGS:
    ""` is not "unset" to it. The escape hatch has to disappear entirely when
    it is empty - emitting it empty is what killed v1.0.1 for KV_MEM.
    """
    assert "EXTRA_ARGS" not in engine_env(values)
    data = engine_env(values, "--set", r"olaresEnv.EXTRA_ARGS=--max-num-seqs\, 4")
    assert data["EXTRA_ARGS"] == "--max-num-seqs, 4"


def test_every_settings_choice_is_a_profile_the_chart_defines() -> None:
    """The manifest's dropdown and the chart's table are two lists, one set.

    Olares validates the value against `options` and then hands it to the
    chart, so a choice the table has no row for is an install that dies at
    render time with a Helm error in the UI.
    """
    manifest = yaml.safe_load((CHART / "OlaresManifest.yaml").read_text())
    chart_values = yaml.safe_load((CHART / "values.yaml").read_text())
    (declared,) = [env for env in manifest["envs"] if env["envName"] == "SERVING_PROFILE"]
    choices = {option["value"] for option in declared["options"]}

    assert choices == set(chart_values["servingProfiles"])
    assert declared["default"] in choices
    assert chart_values["profile"]["serving"] in choices


def llminit_supports(values: Path, *overrides: str) -> set[str]:
    for obj in yaml.safe_load_all(template(values, *overrides)):
        if obj and obj.get("kind") == "Deployment" and obj["metadata"]["name"] == "llminit":
            (container,) = obj["spec"]["template"]["spec"]["containers"]
            (supports,) = [e["value"] for e in container["env"] if e["name"] == "MODEL_SUPPORTS"]
            return set(supports.split(","))
    raise AssertionError("the chart rendered no llminit Deployment")


def test_vision_ships_on_with_the_tower_offloaded(values: Path) -> None:
    """On 24 GB, DFlash2 plus an on-GPU tower OOMs in graph capture upstream.

    So the two knobs ship together; vision without the offload is a boot
    failure on the default profile, not a slower configuration.
    """
    data = engine_env(values)
    assert (data["VISION"], data["VISION_OFFLOAD"]) == ("1", "1")


@pytest.mark.parametrize(("vision", "advertised"), [("1", True), ("0", False)])
def test_llminit_advertises_vision_only_when_the_engine_serves_it(
    values: Path, vision: str, advertised: bool
) -> None:
    """With VISION=0 the engine refuses every image with a 400."""
    supports = llminit_supports(values, "--set-string", f"profile.vision={vision}")
    assert ("supports_vision" in supports) is advertised
    assert "supports_function_calling" in supports


def test_the_engine_image_is_pinned_to_one_upstream_commit(values: Path) -> None:
    """`latest` lets an upstream push change what this box serves on a restart."""
    for obj in yaml.safe_load_all(template(values)):
        if obj and obj.get("kind") == "Deployment" and obj["metadata"]["name"] == "qwen38hyperqwen":
            (container,) = obj["spec"]["template"]["spec"]["containers"]
            repo, _, tag = container["image"].rpartition(":")
            assert repo == "ghcr.io/syv-ai/hyperqwen"
            assert tag.startswith("sha-"), tag
            return
    raise AssertionError("the chart rendered no qwen38hyperqwen Deployment")
