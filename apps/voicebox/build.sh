#!/bin/sh
# Build the Voicebox image from upstream's own Dockerfile at a pinned commit.
#
# NOT REPRODUCIBLE. Unlike routerimages, two builds of this script differ:
# upstream's Dockerfile runs `apt-get update`, resolves backend/requirements.txt
# unhashed, and installs Qwen3-TTS from git HEAD. What is pinned is what this
# script controls: the upstream source (commit + tarball checksum) and the two
# base images (by digest). The chart therefore pins the pushed tag, not a digest
# known in advance. Making it reproducible (Debian snapshot, hashed lock,
# Qwen3-TTS at a commit) is a follow-up.
#
# Usage: apps/voicebox/build.sh <image> [extra podman build args...]
#        apps/voicebox/build.sh --print-tag
# The tag is derived here, so the chart's pin can be checked against it.
set -eu

VOICEBOX_VERSION=0.6.0
VOICEBOX_COMMIT=8af7efe62fab8d33e2a5dfbedabaa45f68a5184f
VOICEBOX_SHA256=3a8b520cc7d4cfe36b5e9dccfd6e399724127e19602df9b1ee8f3c05bcebbf91
# Upstream writes short names, which podman refuses without a TTY to ask on.
BUN_IMAGE=docker.io/oven/bun:1.3@sha256:e10577f0db68676a7024391c6e5cb4b879ebd17188ab750cf10024a6d700e5c4
PYTHON_IMAGE=docker.io/library/python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

tag=$VOICEBOX_VERSION-$(echo "$VOICEBOX_COMMIT" | cut -c1-7)
if [ "$1" = --print-tag ]; then
  echo "$tag"
  exit 0
fi
image=$1
shift

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

curl -fsSL -o "$work/src.tgz" \
  "https://github.com/jamiepine/voicebox/archive/$VOICEBOX_COMMIT.tar.gz"
echo "$VOICEBOX_SHA256  $work/src.tgz" | sha256sum -c -
mkdir "$work/src"
tar xzf "$work/src.tgz" -C "$work/src" --strip-components=1

sed -i \
  -e "s|^FROM oven/bun:1 |FROM $BUN_IMAGE |" \
  -e "s|^FROM python:3.12-slim\$|FROM $PYTHON_IMAGE|" \
  -e "s|^FROM python:3.12-slim |FROM $PYTHON_IMAGE |" \
  "$work/src/Dockerfile"
# Every FROM must have been rewritten; a new upstream stage would slip through.
if grep -n '^FROM' "$work/src/Dockerfile" | grep -v '@sha256:'; then
  echo "unpinned FROM in upstream Dockerfile" >&2
  exit 1
fi

podman build --platform linux/amd64 \
  --label org.opencontainers.image.source=https://github.com/RonnyPfannschmidt/homelab-images \
  --label "org.opencontainers.image.revision=$VOICEBOX_COMMIT" \
  --label org.opencontainers.image.url=https://github.com/jamiepine/voicebox \
  -t "$image:$tag" "$@" \
  -f "$work/src/Dockerfile" "$work/src"
