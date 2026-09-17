#!/usr/bin/env bash
# The server half of the comparison for theresa's sync root, with her mounts
# resolved to the paths they actually live at.
#
# This exists because a sync root is not one server directory. `find` over
# /var/lib/nextcloud/data/theresa/files sees five of her eight top-level
# folders; the other three are mounts the database assembles at request time
# and they live somewhere else entirely:
#
#   geteilt      Team folder 1   __groupfolders/1        (groupfolders mount)
#   geteilt (2)  share id 3      __groupfolders/1        the SAME tree, twice
#   Familie      share id 1      ronny/files/Familie     out of the owner's home
#
# Compare without that and all three read as totally missing locally, which is
# the same shape as real data loss and is not it.
#
# `geteilt` and `geteilt (2)` are deliberately emitted twice from one source.
# Share 3 is a second mount of Team folder 1, so if her client syncs both, the
# 27 GB is on her disk twice and both copies must match the same tree. If share
# 3 is removed, drop that block and the client's copy with it.
#
# .Kollektive/* is omitted: collectives live under appdata, the client does not
# sync a dot-folder, and nothing there is user data that only she holds.
#
# Run on the homeserver, as root. Output is LC_ALL=C sorted, ready for
# compare-manifests.sh.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
manifest="$here/server-manifest.sh"
data=${NEXTCLOUD_DATA:-/var/lib/nextcloud/data}
out=${1:-theresa-server.tsv}

: > "$out"
"$manifest" "$data/theresa/files" >> "$out"
"$manifest" "$data/__groupfolders/1" | sed 's|^|geteilt/|' >> "$out"
"$manifest" "$data/__groupfolders/1" | sed 's|^|geteilt (2)/|' >> "$out"
"$manifest" "$data/ronny/files/Familie" | sed 's|^|Familie/|' >> "$out"

LC_ALL=C sort -o "$out" "$out"

echo "$out: $(wc -l < "$out") entries" >&2
cut -f1 "$out" | cut -d/ -f1 | sort | uniq -c | sort -rn >&2
