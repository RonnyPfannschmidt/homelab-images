#!/bin/sh
# Produce the server half of the comparison, in the format the Windows
# collector produces for the client half.
#
#   ./server-manifest.sh /var/lib/nextcloud/data/theresa/files > server.tsv
#
# The format is fixed by nextcloud-local-manifest.ps1 and the two have to stay
# identical or the diff is worthless. See README.md for the field list; the
# three things that are easy to get wrong and are handled here:
#
#   TZ=UTC        find's %T* conversions print LOCAL time. Without this the
#                 whole manifest is offset by the server's timezone and every
#                 file differs.
#   fraction      %TS prints seconds WITH a fractional part - ext4 keeps
#                 nanoseconds - and there is no find conversion for whole
#                 seconds. The sed strips it. Nextcloud stores and transfers
#                 whole seconds, so the fraction is noise on every line.
#   LC_ALL=C      sort by byte, which is what the collector sorts by. Under a
#                 German locale `sort` ignores case and punctuation weight and
#                 orders umlauts next to their base letters, and the two files
#                 end up in different orders with identical content.
#
# Read-only, like its counterpart: find, and nothing else.
set -eu

root=${1:-}
if [ -z "$root" ] || [ ! -d "$root" ]; then
    cat >&2 <<'USAGE'
usage: server-manifest.sh <directory> [> server.tsv]

The directory is a user's file tree inside the Nextcloud data directory:

    <datadir>/<uid>/files

NOT <datadir>/<uid>, which also holds files_versions, files_trashbin, uploads
and cache - none of which the desktop client ever sees, and all of which would
appear in the diff as thousands of files the client is "missing".

RECEIVED SHARES ARE NOT IN THIS TREE. A share someone else owns is mounted
into the user's view by the database at request time; on disk it lives only in
the owner's directory. The client shows it, this listing does not, and the
whole share therefore reads as missing locally. To cover one, run this against
the owner's copy and prefix the result - see README.md, "Shares".
USAGE
    exit 2
fi

cd "$root"

# %P is the path relative to the starting point with no leading ./ - the same
# thing the collector produces by stripping the sync-folder prefix.
#
# Directories and links carry - for size and mtime rather than their real
# values, deliberately: both sides maintain directory mtimes independently and
# Linux reports a directory's size as a block count where Windows reports
# zero, so recording them would put a difference on almost every directory
# line. Existence is what is being compared.
TZ=UTC find . -mindepth 1 \
    \( -type d -printf '%P\td\t-\t-\n' \
    -o -type l -printf '%P\tl\t-\t-\n' \
    -o        -printf '%P\tf\t%s\t%TY-%Tm-%TdT%TH:%TM:%TSZ\n' \) \
    | sed -E 's/\.[0-9]+Z$/Z/' \
    | LC_ALL=C sort
