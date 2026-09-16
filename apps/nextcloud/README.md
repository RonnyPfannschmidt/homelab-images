# nextcloud

Nextcloud 32.0.8 with the 24 apps the homeserver serves, baked in and pinned.

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
  versions that instance served on 2026-09-16. The first image is the same app
  set, not a newer one.

An app moves when someone moves it:

```
./pin_apps.py --available            # what the app store has that is newer
./pin_apps.py --set memories=7.8.2   # repin one, rewriting apps.lock
./pin_apps.py --check                # re-download every pin, verify every hash
```

The build reads `apps.lock` and nothing else. `pin_apps.py` never runs in CI.

## The image owns the app set

The base image's entrypoint rsyncs `/usr/src/nextcloud` into `/var/www/html` on
every start, and ships `custom_apps` in its exclude list. This image takes it
out of that list, because with it in, apps baked into the image would freeze at
whatever the first start wrote and a newer image would never reach an existing
instance — silently, which is the worst version of it.

The consequence is deliberate: **an app installed through the web UI is removed
again on the next container start.** It lands in `custom_apps`, and the sync
makes `custom_apps` match the image. Experimenting still works; it just does not
survive, and `apps.lock` is how something is meant to stay.

## What it does not carry

Configuration. `config.php` belongs to the instance, and so does every path,
hostname and credential in it.

Three apps are enabled in the homeserver's database with no backend running,
and this image does not add one: `notify_push` (the push server), `whiteboard`
(its websocket server), `app_api` (a deploy daemon). They are inert on the
NixOS instance too. Whether they get one is a deployment decision.
