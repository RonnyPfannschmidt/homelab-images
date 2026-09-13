"""The trust material: does the committed policy still match the keys.

Everything here is cheap and needs no cluster. It exists because the failure
modes it catches are all quiet ones - a policy that trusts a key nobody has,
a key nobody trusts, or a registry configuration that makes a correctly signed
image read as unsigned.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parents[1]
TRUST = REPO_ROOT / "trust"

_spec = importlib.util.spec_from_file_location("render_policy", TRUST / "render_policy.py")
assert _spec and _spec.loader
render_policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render_policy)


def test_policy_matches_the_key_directory() -> None:
    """The committed policy is what `render_policy.py` would write today."""
    committed = (TRUST / "etc" / "containers" / "policy.json").read_text()
    assert committed == render_policy.render(), (
        "trust/etc/containers/policy.json is stale - run trust/render_policy.py. "
        "A policy that has drifted from trust/keys/ either trusts a key nobody "
        "holds or refuses one that is in use."
    )


def test_keys_are_public_keys(tmp_path: Path) -> None:
    """A private key in `trust/keys/` would be published; check the header."""
    for key in sorted((TRUST / "keys").glob("*")):
        text = key.read_text()
        assert "PUBLIC KEY" in text, f"{key.name} is not a public key"
        assert "PRIVATE" not in text, (
            f"{key.name} contains private key material and this repository is public"
        )


def test_keys_are_named_by_when_they_were_minted() -> None:
    """`cosign-YYYY-MM.pub`, so a rotation reads as a rotation in `ls`."""
    import re

    for key in sorted((TRUST / "keys").glob("*.pub")):
        assert re.fullmatch(r"cosign-\d{4}-\d{2}\.pub", key.name), (
            f"{key.name} does not look like cosign-YYYY-MM.pub"
        )


def test_sigstore_attachments_are_enabled_for_the_namespace() -> None:
    """Without this, verification silently finds no signature at all.

    containers/image only looks for a cosign attachment where
    `use-sigstore-attachments` is set, and reports its absence as "unsigned"
    rather than as a configuration problem.
    """
    config = yaml.safe_load(
        (TRUST / "etc" / "containers" / "registries.d" / "ghcr.yaml").read_text()
    )
    scopes = config["docker"]
    assert render_policy.SCOPE in scopes, (
        f"the policy enforces {render_policy.SCOPE} but registries.d does not "
        "enable sigstore attachments for it"
    )
    assert scopes[render_policy.SCOPE]["use-sigstore-attachments"] is True


def test_rotation_keeps_both_keys_acceptable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two keys in the directory means either signature verifies.

    The property the whole rotation procedure rests on: containers/image
    accepts a signature made by *any* key in `keyPaths`. Asserted against a
    fabricated key directory so that it holds before the first real key exists,
    and keeps holding after.
    """
    keys = tmp_path / "keys"
    keys.mkdir()
    for name in ("cosign-2026-09.pub", "cosign-2027-03.pub"):
        (keys / name).write_text("-----BEGIN PUBLIC KEY-----\nx\n-----END PUBLIC KEY-----\n")
    monkeypatch.setattr(render_policy, "KEYS", keys)

    requirement = render_policy.build_policy()["transports"]["docker"][render_policy.SCOPE][0]
    assert requirement["type"] == "sigstoreSigned"
    assert requirement["keyPaths"] == [
        "/etc/pki/containers/cosign-2026-09.pub",
        "/etc/pki/containers/cosign-2027-03.pub",
    ], "both keys must be listed, or the transition period rejects one of them"
    assert "keyPath" not in requirement, (
        "the singular form accepts exactly one key; a rotation would have to "
        "rewrite the policy's shape rather than add a line"
    )


def test_no_keys_enforces_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The state this repository starts in has to be a working one."""
    empty = tmp_path / "keys"
    empty.mkdir()
    monkeypatch.setattr(render_policy, "KEYS", empty)
    docker = render_policy.build_policy()["transports"]["docker"]
    assert render_policy.SCOPE not in docker
    assert docker[""] == [{"type": "insecureAcceptAnything"}]
