# An Olares Market source for these charts

The charts in `olares-apps/` are installable today by uploading a packaged
tarball (`olares-cli market upload`, then `market install -s upload`). That
works and needs nothing here. What it does not give is a Market entry: no app
page, no version that Olares notices has moved, no install without a terminal.

This tool makes the same charts a **market source** — one URL added under
*Market → Settings → Add source*, after which the apps appear in the Market
like any other.

## What a market source actually is

Four endpoints. Not a Helm repository, and not an `index.yaml`:

| Endpoint | Method | What it answers |
|---|---|---|
| `/api/v1/appstore/hash` | GET | one hash; Olares polls it and re-reads the catalog only when it moves |
| `/api/v1/appstore/info` | GET | the whole catalog: one summary per app, plus the categories they are filed under |
| `/api/v1/applications/info` | **POST** | the full record for a list of app ids — the app page, and what the installer reads |
| `/api/v1/applications/<app>/chart` | GET | the packaged chart, `?fileName=<app>-<version>.tgz` |

Olares 1.12.7+ asks under `/api/v2/` first and falls back to `/api/v1/` on a
404; both prefixes are answered.

The protocol is not documented by upstream. It was read off a working
third-party source — [aamsellem/olares-one-market][ref], whose Worker and
catalog builder are public — and the field shapes here follow it. Two of its
findings are load-bearing and fail silently when ignored: timestamps must
carry nine digits of fraction, and the flat `apps` dictionary alone parses to
zero apps unless a ranked `tops` list accompanies it.

[ref]: https://github.com/aamsellem/olares-one-market

## The three pieces

| | |
|---|---|
| [`chart_package.py`](chart_package.py) | `helm package`, without helm: a chart tarball from a directory, honouring `.helmignore`, byte-reproducible |
| [`build_market.py`](build_market.py) | renders `olares-apps/` into a servable tree — the catalog, the three GET responses, the chart tarballs, a landing page |
| [`serve.py`](serve.py) | the service: serves that tree, answers the POST, and rebuilds it when GitHub says `main` moved |

```bash
uv run python tools/olares-market/build_market.py --out site
uv run pytest testing/test_market_build.py testing/test_market_serve.py
```

`--skip-charts` writes the JSON without the tarballs. `--pages-origin` and
`--source-url` decide the URLs the catalog and the landing page carry.

## Two POSTs, and they are not related

```
POST /api/v1/applications/info   Olares asking for app records
POST /hooks/github               GitHub saying a push happened
```

The hook is HMAC-signed (`X-Hub-Signature-256`, constant-time compare), acts
only on a `push` whose `ref` is the configured branch, and answers `202`
*before* doing anything — GitHub gives a hook ten seconds and a build takes
longer. The pull and rebuild then run on a worker thread behind a lock:
hooks arriving during a build coalesce into one more build, a failed sync
leaves the previous tree serving, and the new tree becomes live through an
atomic symlink swap. `--poll-seconds` (default 1800) catches a hook that was
never delivered or arrived while the service was down.

An unsigned hook route would be a remote way to make this account run a build
script, so with no secret configured the route answers 503 rather than
running.

## Running it on uberspace

```
~/olares-market/
  repo/            a normal git checkout of homelab-images
  builds/<sha>/    one generated tree per commit
  current -> builds/<sha>
  webhook-secret   0600, never in git
```

```bash
git clone https://github.com/RonnyPfannschmidt/homelab-images ~/olares-market/repo
openssl rand -hex 32 > ~/olares-market/webhook-secret && chmod 600 ~/olares-market/webhook-secret
```

`~/etc/services.d/olares-market.ini`:

```ini
[program:olares-market]
command=/usr/bin/python3.13 %(ENV_HOME)s/olares-market/repo/tools/olares-market/serve.py --state-dir %(ENV_HOME)s/olares-market --port 8099
autostart=true
autorestart=true
startsecs=30
stopsignal=INT
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
redirect_stderr=true
```

```bash
supervisorctl reread && supervisorctl update
uberspace web domain add olares-market.ronnypfannschmidt.de
uberspace web backend set olares-market.ronnypfannschmidt.de/ --http --port 8099
```

Then in the repository's *Settings → Webhooks*: payload URL
`https://olares-market.ronnypfannschmidt.de/hooks/github`, content type
`application/json`, the secret from the file above, and **just the push
event**. The `ping` GitHub sends on save is answered, so a green tick there
means the signature is right.

The service runs the generator *out of the checkout*, so a push that changes
`build_market.py` takes effect on the next build. A push that changes
`serve.py` needs `supervisorctl restart olares-market` — the running process
is the old file.

The declared side of this — the supervised service and the web backend route
— lives in the private repository's `specs/uberspace.py`, beside gitea.

## Packaging without helm

`chart_package.py` exists because the rebuild has to run where there is no
helm binary, and because `helm package` stamps every entry with the file's
mtime, so its output differs per checkout. Everything here is pinned:
mtime 0, mode 0644, no ownership, no gzip header timestamp. Two packagings of
one commit are the same bytes.

It is not a byte-for-byte reimplementation, and could not be: `helm package`
loads a chart and writes it back out, which rewrites `Chart.yaml` (dropping
quotes, rewrapping long lines) and unpacks a vendored
`charts/<dep>-<version>.tgz` into a directory. This copies the tree as
committed. Both are valid packages, and
`testing/test_market_build.py` pins the equivalence that matters: for every
chart in this repository, the two packagings hold the same files and **render
to identical Kubernetes objects**.

The `.helmignore` semantics are helm's, and they are not git's. A pattern
without a slash matches a *base name* anywhere in the tree; one with a slash
is anchored to the chart root; `*` never crosses a separator; `!` is refused
rather than silently ignored, as helm refuses it. That last set of rules is
not pedantry — `fichtendorf/.helmignore` has to write `/*.tgz` rather than
`*.tgz`, because the loose form would drop the vendored subchart and Olares
never resolves a `dependencies:` entry.

## What this does not change

These charts are not public Market listings and this does not make them any
more of one; see the repository README. A private source is the mechanism for
installing one's own charts onto one's own box.

`updated_at` in the catalog is each chart's last commit date rather than the
build clock, so two builds of one commit produce identical bytes — without
that, every rebuild would publish a new hash and Olares would re-sync every
app on a timer.
