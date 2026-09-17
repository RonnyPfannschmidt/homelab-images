# nextcloud-local-manifest

Capture what a Nextcloud desktop client actually holds on disk, so it can be
compared against the server's data directory.

It exists because a storage-index split on the server left one question open.
The server side is verified intact - no user files lost. What nobody has seen
is the Windows client's copy, and the report from the person using it is
"different file sets on the shares", which is a symptom, not a measurement.
This produces the measurement, from a machine whose owner should not have to
interpret anything: she runs one command, gets one file, and sends it.

| | |
|---|---|
| [nextcloud-local-manifest.ps1](nextcloud-local-manifest.ps1) | runs on her Windows machine; produces the client manifest |
| [server-manifest.sh](server-manifest.sh) | runs on the server; produces the same format from the data directory |
| [compare-manifests.sh](compare-manifests.sh) | splits the difference into the three questions worth asking |

## What she runs

Send her the `.ps1` and this one line. It needs no installation, no admin
rights, and works under the default ExecutionPolicy:

```
powershell -ExecutionPolicy Bypass -File "%USERPROFILE%\Downloads\nextcloud-local-manifest.ps1"
```

`-ExecutionPolicy Bypass` on the command line overrides the machine default
(`Restricted` on a home machine), because a policy set that way is a default,
not an enforcement. It does **not** override a policy pushed by group policy or
an MDM - if that is in force the command fails with
`UnauthorizedAccess` and the fallback is to pipe the script in on standard
input, which ExecutionPolicy does not govern because no file is being run:

```
powershell -Command "Get-Content '%USERPROFILE%\Downloads\nextcloud-local-manifest.ps1' -Raw | Invoke-Expression"
```

Right-click -> *Run with PowerShell* also works and is the least typing. The
script pauses for a keypress at the end precisely because that route closes
the window on exit, and the last thing it prints is the name of the file to
send back.

### The message to paste

German, because that is the language the machine and its owner are in:

```
Hallo! Kannst du mir bitte kurz helfen, etwas zu pruefen?
1. Lade die angehaengte Datei herunter und merke dir, wo sie liegt.
2. Rechtsklick darauf -> "Mit PowerShell ausfuehren".
3. Warte, bis "FERTIG" steht. Das kann ein paar Minuten dauern.
4. Schick mir die Datei, die am Ende genannt wird. Sie liegt auf dem Desktop.
Es wird nichts geaendert oder geloescht - es wird nur nachgeschaut.
```

English, same five lines:

```
Hi! Could you help me check something?
1. Download the attached file and note where it lands.
2. Right-click it -> "Run with PowerShell".
3. Wait until it says DONE. It may take a few minutes.
4. Send me the file it names at the end. It is on your Desktop.
Nothing is changed or deleted - it only looks.
```

## It only reads

This is the property the whole thing depends on, so it is worth stating what
backs it rather than only promising it.

The only calls made against the sync tree are directory enumeration and the
metadata that enumeration already returns - `EnumerateFileSystemInfos`, whose
results carry size, mtime and attributes from the directory scan itself. No
stream is ever opened against a file in there, nothing is created, no
attribute is set. The output is assembled under `%TEMP%` and delivered to the
Desktop, and if the Desktop turns out to be inside a folder about to be
scanned the script falls back to `%USERPROFILE%` and then to `%TEMP%` rather
than writing into the tree it is describing.

Measured on the test tree, taking size, mtime, atime, ctime and mode before
and after a full run: the only thing that changes is the **access time of
directories**, which is what reading a directory listing does and what
Explorer does every time a window is opened. No file's access time changes -
which is the specific evidence that no file content was read, and therefore
that no Files-On-Demand placeholder was hydrated by the walk.

## The manifest format

One record per line. Tab separated, UTF-8 **without a BOM**, **LF** endings,
sorted by the UTF-8 bytes of the whole line.

```
<relative path>	<type>	<size>	<mtime>
```

- **path** - relative to the sync folder, forward slashes, no leading `./`
- **type** - `f` file, `d` directory, `l` junction or symlink
- **size** - bytes for a file, `-` for anything else
- **mtime** - UTC, whole seconds, ISO 8601 with `Z`; `-` for anything but a file

Every one of those choices is there to stop a difference that is not a
difference from reaching the diff:

- **No BOM.** `Set-Content -Encoding UTF8` on PowerShell 5.1 writes one, and
  those three bytes land at the head of record one, where they make the first
  line differ for reasons that have nothing to do with the files.
- **LF, not CRLF.** The obvious one, and the one that turns a clean comparison
  into ten thousand differing lines.
- **Byte sort, not culture sort.** PowerShell's `Sort-Object` is culture-aware:
  under a German locale `Ähre` sorts next to `Ahre`, where `LC_ALL=C sort`
  puts it after `zzz`, because `0xC3` is greater than `z`. Even an ordinal
  sort is not enough, since ordinal compares UTF-16 code units and those
  reorder astral characters relative to their UTF-8 encoding. The script
  therefore sorts on each line's UTF-8 bytes, decoded through Latin-1 so that
  an ordinal comparison *is* a byte comparison - which is exactly the order
  `LC_ALL=C sort` produces on the other side.
- **Whole seconds.** NTFS keeps 100 ns ticks, ext4 keeps nanoseconds, and
  `find -printf '%TS'` prints the fraction. Nextcloud stores and transfers
  whole seconds, so anything finer is noise on every single line.
- **Path first.** So that sorting whole lines and sorting by path are the same
  operation on both sides, and `join` can be pointed at field 1 without either
  side being re-sorted.
- **No size or mtime on a directory.** Both sides maintain those independently
  - the server's PHP writes touch directories the client never does, and Linux
  reports a directory's size as a block count where Windows reports zero - so
  recording them would put a difference on nearly every directory line and
  bury the ones that mean something. For a directory, existence is the claim.

Verified: on a 10 200-entry tree containing umlauts, an eszett, a space in a
path and an NFD-composed name, the PowerShell collector and `server-manifest.sh`
produce **byte-identical** files.

## The server side

```bash
tools/nextcloud-local-manifest/server-manifest.sh \
    /var/lib/nextcloud/data/theresa/files > server.tsv
```

Point it at `<datadir>/<uid>/files`, not at `<datadir>/<uid>` - the latter also
holds `files_versions`, `files_trashbin`, `uploads` and `cache`, none of which
the client ever sees and all of which would appear as thousands of files it is
"missing".

It is `find` and nothing else. The three things it gets right that a hand-typed
`find` usually does not: `TZ=UTC`, because `%T*` prints local time and without
it the entire manifest is offset by the server's timezone; a `sed` stripping
the fractional second, because there is no `find` conversion for whole seconds;
and `LC_ALL=C sort`, for the reason above.

### Shares are not in her data directory

This is the trap, and given the reported symptom it is probably the answer.

**A share that someone else owns is not on disk in the recipient's tree.** It
lives in the owner's directory and is mounted into the recipient's view by the
database at request time. The desktop client shows it as an ordinary folder;
`find` over her data directory does not see it at all. Compare the two without
knowing that and every shared folder reads as totally missing locally - which
is exactly the shape of "different file sets on the shares".

`compare-manifests.sh` prints the top-level entries that are server-only for
this reason: each one is either genuinely missing or a share. To cover a share,
run the manifest against the owner's copy and prefix the paths with the name
the recipient sees it under:

```bash
tools/nextcloud-local-manifest/server-manifest.sh \
    /var/lib/nextcloud/data/ronny/files/Urlaubsfotos \
    | sed 's|^|Urlaubsfotos/|' >> server.tsv
LC_ALL=C sort -o server.tsv server.tsv
```

The mount name and the owner's path differ whenever the recipient has renamed
the share, so the prefix is a per-share judgement, not something to automate
blind.

**Capture before repairing.** A storage-index repair - `occ files:scan`,
`occ files:cleanup` - changes the thing being measured. The filesystem listing
survives it, but the question "what did the client see" does not.

## Comparing

```bash
tools/nextcloud-local-manifest/compare-manifests.sh \
    server.tsv manifest-01-Nextcloud.tsv comparison/
```

Three files, because they are three different problems:

| | |
|---|---|
| `only-on-server.tsv` | the server has it, the machine does not - data she cannot reach |
| `only-on-client.tsv` | the machine has it, the server does not - **the only ones that can still be lost**, since nothing else holds them |
| `differing.tsv` | both have it, size or mtime disagree - a conflict or a half-finished transfer |

A plain `diff -u` of the two manifests answers all three at once and is
unreadable as soon as the answer is large, because one missing directory shifts
every line beneath it. If you want it anyway:

```bash
diff -u server.tsv manifest-01-Nextcloud.tsv | less
```

What the comparison folds, and why each fold is done there rather than at
capture time - the manifests are evidence, and filtering while capturing makes
"the tool hid it" indistinguishable from "it was not there":

- **Unicode normalisation to NFC.** macOS writes NFD, Windows and Linux
  normally write NFC, and Nextcloud normalises nothing in transit. The same
  name can therefore be two different byte sequences and diff as two unrelated
  files that look identical on screen. The report counts non-NFC names; if that
  count is not zero, this fold is load-bearing.
- **Client-only files.** `.sync_*.db` and its WAL, the sync logs, `desktop.ini`,
  `Thumbs.db`, `.DS_Store`. The journal is the one worth a glance before
  discarding it: a sync folder with no journal is a folder the client has never
  successfully synced.
- **The `.nextcloud` / `.owncloud` suffix.** The client's older virtual-files
  mode names a placeholder `foo.pdf.nextcloud` where the server has `foo.pdf`.
  Unfolded, each one is one missing file plus one extra file.

## Virtual files, and why they get their own file

With Files On-Demand ("virtual files") switched on, a file that has never been
opened is a placeholder: **its metadata carries the real size and the real
mtime, and none of its bytes are on the disk.** It diffs perfectly clean
against the server while holding nothing - which is normal, expected, and
completely invisible in a manifest that only records path, size and mtime.

So the manifest stays four columns and says nothing about it - a fifth would
break the symmetry with `find -printf` and the diff with it - and cloud state
goes to `cloud-state-NN-<folder>.tsv`, sorted the same way, only written when
there is something to write:

```
# path	state	logical_size	size_on_disk	pin	attributes
Scans/Vertrag.pdf	dehydrated	184320	0	unpinned	0x00420020
Scans/Bericht.pdf	partial	93827509	4194304	-	0x00420020
Scans/Notiz.pdf	local	14705	16384	pinned	0x00080020
```

Every cloud-managed file is listed, not only the dehydrated ones, because the
interesting questions are comparative:

- **`state`** - `dehydrated` (no bytes here), `partial` (some), `local` (all),
  `unknown` (the allocation could not be read). `partial` is arithmetic, not an
  attribute: a provider sets `RECALL_ON_DATA_ACCESS` on anything it might have
  to fetch, including a file it has already fetched half of, so the attribute
  alone cannot separate "nothing here" from "half here". Only comparing
  `size_on_disk` against `logical_size` can, which is what an interrupted
  download looks like.
- **`size_on_disk`** - what the volume actually allocated, via
  `GetCompressedFileSizeW`. That call reads the directory entry; *opening* the
  file is what would hydrate it, and nothing here opens anything. `-` means the
  lookup failed, and `report.txt` then says how many times, because a whole
  column of `-` with no explanation is exactly the kind of silently wrong
  evidence this tool exists to avoid.
- **`pin`** - `pinned` ("Always keep on this device"), `unpinned` ("Free up
  space"), or `-` for neither. This is the difference between a file that is
  not here because somebody chose that and one the client evicted on its own.
- **`attributes`** - the raw attribute word. Last column and deliberately
  redundant: whatever this tool failed to anticipate can be recovered from the
  output instead of needing the machine a second time.

`report.txt` carries the counts, the byte total the placeholders account for,
and the `virtualFilesMode` read out of the client configuration - `off` means
every file is genuinely on the disk and the question does not arise.

The distinction matters for what happens next: a placeholder is safe if the
server has the file and lost data if it does not. "Only on the client" is the
column that cannot be a placeholder, and it is the only column where the bytes
exist nowhere else.

## What else the report carries

Client version (from the installed binary, then the registry, then
`nextcloud.cfg` - in that order, because the configuration records the version
that last wrote it and is stale across an upgrade), every configured sync
folder with its remote target and virtual-files mode, per-folder counts of
files, directories, links, bytes, placeholders, suffix stubs and non-NFC names,
any directory that could not be read, and every warning the run produced.

An empty folder is reported as a warning rather than a quiet zero: an empty
manifest compared against a populated server reads as total data loss, and is
far more often the wrong path.

## Notes on the Windows side

- **The script is deliberately pure ASCII and has to stay that way.** Windows
  PowerShell 5.1 decodes a `.ps1` with no byte-order mark as the system ANSI
  codepage, so a single umlaut in a message here reaches the screen as mojibake
  on exactly the machine this is for. The alternative was committing a BOM. This
  says nothing about file *names*: those never pass through the parser, arriving
  from the filesystem as UTF-16 and leaving through an explicitly chosen UTF-8
  encoder.
- **Long paths.** Enumeration goes through the `\\?\` prefix, which turns off
  `MAX_PATH`. Without it the walk stops partway through a deep tree with "path
  too long", and the result is a manifest that is silently short - the worst
  failure this tool could have. If .NET rejects the prefix the run continues
  without it and says so in the report. The prefix also disables forward-slash
  translation, which is why paths out of `nextcloud.cfg` are converted to
  backslashes before use.
- **`nextcloud.cfg` is QSettings INI**, which escapes non-ASCII as `\xHHHH`, up
  to four hex digits. The ambiguity that implies is not real: the writer also
  escapes a character that follows an escape and is itself a hex digit, so
  `Grüße` is stored as `Gr\xfc\xdf\x65` and never as `Gr\xfc\xdfe`.
- **Every configured folder is walked, not the first.** "Different file sets on
  the shares" is a statement about more than one folder.
- **Junctions and symlinks are recorded but not descended into**, because one
  pointing at an ancestor turns the walk into a loop. A directory carrying a
  reparse point *and* a cloud attribute is an ordinary Files-On-Demand
  directory and is descended into normally.

## Testing it before she runs it

There is one attempt at this on her machine, so the collector has been run
against a synthetic tree - umlauts, eszett, NFD names, a space in a path,
10 200 entries - and its output compared byte for byte with `server-manifest.sh`
over the same tree. [.github/workflows/tools.yml](../../.github/workflows/tools.yml)
is a proposal that does the same on a real `windows-latest` runner, where the
Windows-only paths (`\\?\`, the cloud attributes, `Compress-Archive`) actually
execute.
