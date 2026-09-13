#!/usr/bin/env python3
"""Generate `trust/etc/containers/policy.json` from the keys in `trust/keys/`.

The policy is committed rather than generated at build time so that it is
reviewable in a diff - "this commit started trusting a second key" is exactly
the change that should be obvious. `testing/test_trust.py` fails if the
committed file and the key directory have drifted apart.

Two deliberate choices:

**`keyPaths`, plural, always.** containers/image accepts a signature made by
*any* key in that list, which is the whole rotation mechanism: during a
transition the old and the new key are both present, every node accepts both,
and neither the signing side nor the verifying side has to change at the same
instant as the other. Using the singular `keyPath` for the common one-key case
would mean the rotation procedure starts by rewriting the policy's shape, so it
is plural even with one key.

**The default stays permissive.** `containers-policy.json(5)` recommends
`"default": [{"type": "reject"}]`, and for a single-purpose server image that is
right. These images include a workstation layer that pulls arbitrary containers,
so a global reject would break it for no security gain - what matters is that
*our* namespace cannot be substituted. Tightening the default belongs with the
rpi and olares images, where the set of things the host may pull is small and
known.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
KEYS = HERE / "keys"
POLICY = HERE / "etc" / "containers" / "policy.json"

#: Where the keys land inside the image. `/etc/pki/containers` is where Fedora
#: puts trust material of this kind; the policy refers to these paths, so they
#: are part of the contract between this file and every Containerfile.
KEYS_IN_IMAGE = "/etc/pki/containers"

#: The namespace whose images must be signed. Scoped rather than global: a
#: substitution attack on our own images is the threat this answers.
SCOPE = "ghcr.io/ronnypfannschmidt"


def trusted_keys() -> list[str]:
    """Every `*.pub` in `keys/`, sorted, as in-image absolute paths."""
    return [f"{KEYS_IN_IMAGE}/{p.name}" for p in sorted(KEYS.glob("*.pub"))]


def build_policy() -> dict[str, Any]:
    policy: dict[str, Any] = {
        "default": [{"type": "insecureAcceptAnything"}],
        "transports": {
            "docker-daemon": {"": [{"type": "insecureAcceptAnything"}]},
        },
    }
    if keys := trusted_keys():
        policy["transports"]["docker"] = {
            SCOPE: [
                {
                    "type": "sigstoreSigned",
                    "keyPaths": keys,
                    # cosign signatures carry a repository and no tag, so
                    # matchRepository is the only identity rule that can
                    # accept them at all - see containers-policy.json(5).
                    "signedIdentity": {"type": "matchRepository"},
                }
            ],
            "": [{"type": "insecureAcceptAnything"}],
        }
    else:
        # No keys minted yet. Everything still works; nothing is enforced.
        policy["transports"]["docker"] = {"": [{"type": "insecureAcceptAnything"}]}
    return policy


def render() -> str:
    return json.dumps(build_policy(), indent=4) + "\n"


if __name__ == "__main__":
    rendered = render()
    if "--check" in sys.argv:
        current = POLICY.read_text() if POLICY.exists() else ""
        if current != rendered:
            print("policy.json is stale; run trust/render_policy.py", file=sys.stderr)
            raise SystemExit(1)
        print("policy.json is current")
    else:
        POLICY.write_text(rendered)
        print(f"wrote {POLICY} with {len(trusted_keys())} trusted key(s)")
