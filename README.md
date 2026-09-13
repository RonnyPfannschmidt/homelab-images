# homelab-images

Bootable container images and Helm charts for a two-node home cluster — a
Raspberry Pi 4 and an Olares One box — published to `ghcr.io`.

Everything here is built from a Fedora bootc base and deployed with Flux. The
charts are plain Kubernetes underneath: they were written for
[Olares](https://olares.com) and carry an `OlaresManifest.yaml`, but nothing in
them requires it, which is what makes them testable on a throwaway `kind`
cluster.

## Layout

| | |
|---|---|
| `bootc/` | one directory per bootable image; each is a `Containerfile` on a `quay.io/fedora/fedora-bootc` or `fedora-silverblue` base |
| `olares-apps/` | Helm charts, one per app, each with an `OlaresManifest.yaml` for the Olares market |
| `testing/` | chart render checks, and a `kind`-based rehearsal of the real deployment |
| `trust/` | the cosign verification keys every image carries, and the `policy.json` generated from them |

## Why this repo is separate

It is the public half of a private homelab configuration repository. The split
is not cosmetic:

- **Charts stay generic.** Values arrive at deploy time through
  `HelmRelease.spec.valuesFrom`, from a Secret the private side creates. No
  chart here contains a hostname, a credential or a placeholder shaped around
  one particular cluster — which is the property that makes them worth
  publishing at all.
- **Flux needs no credential to read this.** A `GitRepository` pointing at a
  public repository takes no `secretRef`, so there is no deploy key on this
  side to rotate or leak.
- **Images published from a public repository are public**, so
  `bootc upgrade` on a node needs no registry pull secret.

## Signing

Every image is signed twice: keylessly, binding it to the workflow and commit
that produced it, and with a key, because that is the only form
`/etc/containers/policy.json` can enforce on a node. How to verify an image
yourself, and why a rotation is an ordered procedure rather than a flag flip,
is in [docs/signing.md](docs/signing.md).

## Testing

Two tiers, because they cost very different amounts:

```bash
uv run pytest testing/ -v                 # renders every chart, seconds, no cluster
KIND_TESTS=1 uv run pytest testing/ -v    # adds a real kind cluster, minutes
```

The render tier runs `helm lint`, templates each chart, and validates every
rendered object against the upstream Kubernetes schemas. It needs `helm` and
nothing else. The values it renders against are generated from each chart's own
`values.yaml` rather than written out by hand, so a workload added later cannot
quietly escape them.

The `kind` tier installs each chart into a throwaway cluster and reads the
applied objects back, to check that values actually reached the workload rather
than merely rendering. With `FLUX_TESTS=1` it goes one step further and
rehearses the real deployment path — Flux pulls *this repository at the commit
under test*, joins it to a Secret standing in for the private side, and the
test asserts the resulting Deployment carries values from both sources.

Images are never pulled: charts are installed with waiting disabled and the
workload images replaced by `registry.k8s.io/pause`, because what is under test
is the wiring, not whether a 20 GB model server starts.

## Attribution

Large parts of this repository, including this README, were written by an AI
agent (Claude) working under my direction, and reviewed by me before publishing.
