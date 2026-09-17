#!/bin/sh
# Compare a server manifest with a client manifest and split the difference
# into the three questions worth asking separately.
#
#   ./compare-manifests.sh server.tsv manifest-01-Nextcloud.tsv out/
#
# Writes into the output directory (default ./comparison):
#
#   only-on-server.tsv   the server has it, this machine does not
#   only-on-client.tsv   this machine has it, the server does not
#   differing.tsv        both have it, size or mtime disagree
#
# A plain `diff -u` of the two manifests answers all three at once and is
# unreadable when the answer is large, because one missing directory shifts
# every line under it. Splitting by path first keeps each answer the size of
# the thing it describes.
set -eu

server=${1:-}
client=${2:-}
out=${3:-comparison}

if [ -z "$server" ] || [ -z "$client" ] || [ ! -f "$server" ] || [ ! -f "$client" ]; then
    cat >&2 <<'USAGE'
usage: compare-manifests.sh <server.tsv> <client.tsv> [output-directory]

server.tsv comes from server-manifest.sh, client.tsv out of the zip the
Windows collector produces.
USAGE
    exit 2
fi

mkdir -p "$out"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# Files that exist only because a client is running, and that the server has
# never held. Left in the manifests on purpose - the manifests are evidence,
# and filtering at capture time means being unable to tell "the tool hid it"
# from "it was not there" - and filtered here, where the choice is visible.
#
# .sync_*.db is the client's own state database, and it is the one file whose
# presence is worth checking by hand before discarding it: a sync folder with
# no journal is a folder the client has never successfully synced.
#
# Matched against the PATH FIELD, never against the whole line. An earlier
# version anchored these with $ and fed them whole records to grep, so every
# pattern without a trailing wildcard - desktop.ini among them - silently
# matched nothing, because the line does not end at the file name.
#
# [.] rather than \. throughout: awk -v applies string-escape processing to
# the value before it ever becomes a regexp, so a backslash here arrives at
# the matcher already eaten and gawk warns about it on every run.
noise='(^|/)([.]sync_[^/]*[.]db[^/]*|[.]_sync_[^/]*[.]db[^/]*|[.]owncloudsync[.]log[^/]*|[.]nextcloudsync[.]log[^/]*|desktop[.]ini|Thumbs[.]db|[.]DS_Store)$'

# Unicode normalisation. macOS writes NFD, Windows and Linux normally write
# NFC, and Nextcloud normalises nothing on the way through - so the same name
# can be two different byte sequences on the two sides and diff as two
# unrelated files that look identical on screen. Folding both sides to NFC is
# safe here because the manifests keep the unfolded truth.
if command -v python3 >/dev/null 2>&1; then
    nfc() { python3 -c 'import sys,unicodedata
for line in sys.stdin.buffer:
    sys.stdout.buffer.write(unicodedata.normalize("NFC", line.decode("utf-8", "surrogateescape")).encode("utf-8", "surrogateescape"))'; }
else
    echo "note: no python3, skipping unicode normalisation" >&2
    nfc() { cat; }
fi

# tr -d '\r': the client file is written with LF endings, but it travels by
# mail and through a zip, and something along that path may have "helpfully"
# converted it. A stray CR attaches to the mtime field and makes every line
# differ.
#
# The .nextcloud / .owncloud suffix is the client's old virtual-files mode:
# the file is called foo.pdf on the server and foo.pdf.nextcloud here, so
# without this each one shows up as one missing file plus one extra file.
prepare() {
    tr -d '\r' < "$1" \
        | nfc \
        | awk -F '\t' -v noise="$noise" 'BEGIN { OFS = "\t" }
            $1 ~ noise { next }
            { sub(/[.](nextcloud|owncloud)$/, "", $1); print }' \
        | LC_ALL=C sort
}

prepare "$server" > "$work/server"
prepare "$client" > "$work/client"

cut -f1 "$work/server" | LC_ALL=C sort -u > "$work/server.names"
cut -f1 "$work/client" | LC_ALL=C sort -u > "$work/client.names"

LC_ALL=C comm -23 "$work/server.names" "$work/client.names" > "$out/only-on-server.tsv"
LC_ALL=C comm -13 "$work/server.names" "$work/client.names" > "$out/only-on-client.tsv"

# Metadata disagreements, for the paths both sides have. Directory records
# carry - for size and mtime on both sides by construction, so only files can
# land here.
LC_ALL=C join -t "$(printf '\t')" -j 1 "$work/server" "$work/client" \
    | awk -F '\t' 'BEGIN { OFS = "\t" }
        $2 != $5 || $3 != $6 || $4 != $7 {
            print $1, "server", $2, $3, $4
            print $1, "client", $5, $6, $7
        }' > "$out/differing.tsv"

printf '\n'
printf 'server manifest   %8d records  (%s)\n' "$(wc -l < "$work/server")" "$server"
printf 'client manifest   %8d records  (%s)\n' "$(wc -l < "$work/client")" "$client"
printf '\n'
printf 'only on server    %8d  -> %s\n' "$(wc -l < "$out/only-on-server.tsv")" "$out/only-on-server.tsv"
printf 'only on client    %8d  -> %s\n' "$(wc -l < "$out/only-on-client.tsv")" "$out/only-on-client.tsv"
printf 'size/mtime differ %8d  -> %s\n' "$(( $(wc -l < "$out/differing.tsv") / 2 ))" "$out/differing.tsv"
printf '\n'
printf 'Top-level entries only on the server - each one is either genuinely\n'
printf 'missing here, or a share whose files live in another account:\n'
cut -d/ -f1 "$out/only-on-server.tsv" | LC_ALL=C sort -u | sed 's/^/  /'
printf '\n'
