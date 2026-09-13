# Verifying these images

Every image published from this repository is signed twice, because the two
signatures answer to different consumers.

## Keyless — provenance anyone can check

No key exists. The build job holds a GitHub OIDC token, trades it to Fulcio for
a certificate valid for ten minutes, signs, and records the entry in Rekor. The
certificate's identity is the workflow file and the ref it ran on, so what the
signature attests is "this image was produced by that workflow at that commit",
which is a stronger claim than "somebody who holds a key made it".

```bash
cosign verify \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp \
    '^https://github.com/RonnyPfannschmidt/homelab-images/\.github/workflows/' \
  ghcr.io/ronnypfannschmidt/fedora-basesetup@sha256:...
```

Use the regexp form. The identity carries the ref, so it ends `@refs/heads/main`
today and `@refs/tags/v1` on a release, and an exact match breaks the first time
that changes.

## Keyed — the one a machine can enforce

`podman` and `bootc` decide whether to accept an image using
`/etc/containers/policy.json`, and that file's Fulcio support can only match a
certificate's `subjectEmail`. A GitHub Actions certificate has an empty subject
and a URI SAN, so **a keyless signature cannot be expressed as a policy
requirement at all**. Enforcing `bootc upgrade` therefore needs an ordinary
key pair, and that is what the second signature is.

The public halves live in [`trust/keys/`](../trust/keys), one file per trusted
key. Verify against whichever one you have:

```bash
cosign verify --key trust/keys/cosign-YYYY-MM.pub \
  ghcr.io/ronnypfannschmidt/fedora-basesetup@sha256:...
```

## How a node is configured

Each bootc image copies [`trust/etc/`](../trust/etc) into its own `/etc`, so a
running system carries:

- `/etc/pki/containers/cosign-*.pub` — every trusted verification key;
- `/etc/containers/policy.json` — generated from those keys by
  `trust/render_policy.py`, requiring a `sigstoreSigned` signature for anything
  under `ghcr.io/ronnypfannschmidt`;
- `/etc/containers/registries.d/ghcr.yaml` — `use-sigstore-attachments: true`,
  without which containers/image never looks for the signature and reports a
  correctly signed image as unsigned.

The policy uses `keyPaths`, the plural form, which accepts a signature made by
**any** key in the list. That is deliberate and it is the entire rotation
mechanism: during a transition both the outgoing and the incoming key are
present, and no node has to change trust at the same instant that CI changes
what it signs with.

The consequence worth internalising: **the image a node is running carries the
keys that verify the next image it will install.** Trust moves forward one
upgrade at a time, so a rotation is an ordered procedure, not a flag flip. If a
new key is introduced in the same image that is first signed with it, every node
rejects that image — including the one that would have taught it the new key,
on a machine that may be somewhere you cannot reach.

The procedure for minting and rotating keys lives with the private
configuration repository, because it involves the sops store that holds the
private halves.

## Current state

`trust/keys/` may legitimately be empty. Until the first key is minted the
policy enforces nothing, images are still signed keylessly, and the keyed steps
in the build workflow skip themselves. Adding a key is what turns enforcement
on.
