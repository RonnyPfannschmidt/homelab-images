# Trust material

What a node needs on disk to decide whether an image from
`ghcr.io/ronnypfannschmidt` is one of ours.

- `keys/*.pub` — **every** currently trusted cosign verification key, one file
  per key, named `cosign-YYYY-MM.pub` after the month it was minted. More than
  one is the normal state during a rotation, not an error.
- `etc/containers/policy.json` — generated from `keys/`, never edited by hand.
  `render_policy.py` regenerates it and `testing/test_trust.py` fails if the
  committed file and the key directory disagree.
- `etc/containers/registries.d/ghcr.yaml` — without this, verification cannot
  find the signatures at all. containers/image only looks for a cosign
  signature attachment when `use-sigstore-attachments` is on for the registry,
  and the failure when it is off reads as "unsigned", not as "misconfigured".

Every bootc image here copies `trust/etc/` into `/etc`, so the running image
carries the keys that verify the *next* image. That is what makes rotation an
ordered procedure rather than a flag flip — see `docs/signing.md`.

**An empty `keys/` is a valid state**, and the one this repository starts in:
the policy then requires nothing, images are still signed keylessly, and
nothing breaks. Adding the first key is what turns enforcement on.
