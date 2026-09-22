# homelab-images

Bootable container images, application images and Helm charts for a two-node
home cluster — a Raspberry Pi 4 and an x86 GPU box — published to `ghcr.io`.

Everything here is built from a Fedora bootc base and deployed with **k3s +
Flux**. That is the point of the repository: it is the app layer of a
[replacement for Olares OS](#these-are-our-charts-and-the-manifests-are-transitional), not an
Olares app repository. Charts are plain Kubernetes and are configured through
Helm values, and where an upstream chart exists it is imported and configured
rather than re-written here.

## Layout

| | |
|---|---|
| `bootc/` | one directory per bootable image; each is a `Containerfile` on a `quay.io/fedora/fedora-bootc` or `fedora-silverblue` base |
| `apps/` | one directory per application image — ordinary containers, not bootable ones, built for amd64 and arm64 and joined into one manifest list |
| `olares-apps/` | Helm charts, one per app; upstream charts imported and configured where one exists. The directory keeps its name until the Olares box is reinstalled; see below |
| `tools/` | operator tools that are not images: one directory each, self-contained, documented beside the code |
| `testing/` | chart render checks, and a `kind`-based rehearsal of the real deployment |
| `trust/` | the cosign verification keys every image carries, and the `policy.json` generated from them |

## These are our charts, and the manifests are transitional

Every chart here is maintained in this repository. None of them is a public
Olares Market listing, and the Market is not a constraint on any of them — the
files that only existed to submit one (`owners`, `i18n/`) are gone. Two of the
qwen charts began as copies of aamsellem's `olares-one-market` equivalents and
have since diverged; they are credited in their descriptions, not tracked.

What each chart still carries is an `OlaresManifest.yaml`, and only for as long
as the GPU box runs Olares OS: that is how `olares-apps helm-upgrade` installs
a chart onto it. **The manifests are not the point of the repository and
nothing new should grow one**, but while they exist they are kept *in sync with
the chart beside them*, so the live box stays serviceable until it is
reinstalled.

The replacement it is being reinstalled onto is k3s + Flux + Traefik +
Authelia, where none of this exists: an `entrances[]` entry becomes an Ingress
with forward-auth, `spec.accelerator` becomes ordinary resource requests, and
`.Values.userspace.appData` becomes a PVC. When the box is reinstalled the
manifests go in one commit and the directory is renamed.

Until then: change a chart, change its manifest in the same commit.

There is one thing the manifests now also feed, and it is not a public
listing: [`tools/olares-market/`](tools/olares-market/README.md) renders the
charts into a *private* market source - four HTTP endpoints, served by a small
stdlib service that pulls a checkout when a GitHub webhook fires - so the box
can install and upgrade these apps from the Market UI instead of an uploaded
tarball. It reads the manifests that are here anyway and adds no obligation to
them; when the box is reinstalled, it goes with them.

It also means **no helm binary is needed to package a chart**:
`tools/olares-market/chart_package.py` does it in the standard library, and
the test suite holds it to rendering what `helm package` renders.

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

## The gate

`pre-commit` gates every commit. Install it once and a commit that fails it
does not happen:

```bash
pre-commit install
pre-commit run -a       # pre-flight the whole tree
```

It runs ruff, `mypy --strict`, actionlint (which shellchecks every `run:`
block), hadolint, and the chart render tier below. Two trees are deliberately
exempt and [.pre-commit-config.yaml](.pre-commit-config.yaml) says why:
`olares-apps/*/templates/` is Helm, not YAML, and `olares-apps/*/files/` is
bytes whose hashes packwiz records.

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
