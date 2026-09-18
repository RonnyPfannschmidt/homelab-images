# nextcloud

Nextcloud 32.0.8 with the 9 apps the homeserver serves, baked in and pinned.

`ghcr.io/ronnypfannschmidt/nextcloud:sha-<commit>` — amd64 and arm64 in one
manifest list. Pin a deployment to a `sha-` tag; `latest` moves.

Self-contained: the apps live in `custom_apps`, which the base image already
declares in its own `apps_paths` and already serves. A fresh install comes up
with the full app set and nothing has to be configured to find it.

## What is pinned, and by whom

Nothing here resolves at build time:

- the **base image by digest**, not by tag, in the
  [Containerfile](Containerfile) — `32.0.8-apache` is what the NixOS instance
  runs, and the tag will move under it;
- every **app by version and sha256**, in [apps.lock](apps.lock), at the
  versions that instance served on 2026-09-16, minus the fifteen two review
  passes dropped — `apps.lock`'s own header says which and why.

An app moves when someone moves it:

```
./pin_apps.py --available            # what the app store has that is newer
./pin_apps.py --set calendar=6.2.3   # repin one, rewriting apps.lock
./pin_apps.py --check                # re-download every pin, verify every hash
```

The build reads `apps.lock` and nothing else. `pin_apps.py` never runs in CI.

## The webroot is baked, and there is no rsync

The base image ships an **empty** `/var/www/html`, declares it a `VOLUME`, and
keeps the real 2.0 GB at `/usr/src/nextcloud` for its entrypoint to rsync
across on every start. Measured, neither setting of that works once the content
is baked: an anonymous volume pays a genuine 2.0 GB copy per start (it is not
reflinked, even on btrfs), and a named volume is populated **once** and never
re-synced, so a newer image never reaches it.

A `VOLUME` an ancestor declared cannot be un-declared, so this image stops using
that path. The webroot is **`/var/www/nextcloud`**, served read-only straight
out of the image layer. `/usr/src/nextcloud`, `/entrypoint.sh`, `/cron.sh` and
`/upgrade.exclude` are deleted, not bypassed.

Consequences worth knowing:

- **`config/` and `data/` are mount points.** The image does not populate them
  and has no fallback if they are missing — a container started without them
  fails instead of quietly writing state into a layer about to be discarded.
  The config directory is deliberately emptied of upstream's `*.config.php`
  templates, so the image cannot behave differently from the deployment.
- **An app cannot be installed through the web UI at all.** There is no
  writable apps path any more. Previously the install worked and the next
  start's rsync removed it. `apps.lock` is the only way an app moves.
- **The front-controller rewrite rules live in the image's vhost**, not in
  `.htaccess`, which is read-only and part of the signed core.
  `RewriteOptions Inherit` is what keeps the shipped `.htaccess` rules ahead of
  them; without it Apache discards one set or the other.

## The upgrade is a role, not a side effect of starting

Upstream runs `occ upgrade` from inside the web container's start, which makes
an image bump and an irreversible schema migration the same event — and
afterwards the old image refuses to start, so the rollback window is zero.

Here they are separate:

```
nextcloud-role web        starts; says where code and database versions stand;
                          serves Nextcloud's "upgrade required" page if behind
nextcloud-role upgrade    runs occ upgrade. This is the irreversible step.
nextcloud-role versions   prints the comparison and exits
nextcloud-role cron       one cron.php run, as www-data
nextcloud-role occ …      occ, as www-data
nextcloud-role notify-push --redis-url … --nextcloud-url …
                          the push server, from the binary the app ships
```

`notify-push` is a role rather than another image because the server and the
app have to be the same version, and baking the app already bakes the binary.
It reads the database connection from **`config.php` only** — not from the
`*.config.php` overrides Nextcloud merges afterwards — so `--redis-url` and
`--nextcloud-url` are the caller's job and the role guesses neither.

`web` refuses to start outright if the code is *older* than the database, where
Nextcloud would otherwise 500 in a loop.

**Across a major version, move `apps.lock` and the base digest in the same
commit.** `occ upgrade` disables every app whose `info.xml` caps below the new
server version and does not re-enable them.

## What it does not carry

Configuration. `config.php` belongs to the instance, and so does every path,
hostname and credential in it.

A backend, where an app needs one. `richdocuments` talks to a Collabora
container the deployment runs, and this image does not bake it. `notify_push`
used to be on that list and is not any more: see the role above. `whiteboard`
was the third, with a websocket server of its own; it is gone as of
2026-09-18, and its container and podman secret go with it.

An app baked in here with no backend configured is the failure mode to watch
for, because nothing reports it and the app looks installed either way. Three
have now been dropped for exactly that: `onlyoffice` sat enabled for years with
no `DocumentServerUrl` at all, behind the Collabora that was actually serving;
`integration_paperless` had no server URL or token; `sociallogin` was fully
configured with OAuth providers and had never carried a single login.

The lesson those three share is that *enabled* says nothing. The only reliable
test is whether the app's own tables and configuration hold anything — which is
a question for the instance, not for this file.
