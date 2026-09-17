<#
.SYNOPSIS
    Record what a Nextcloud desktop client actually has on disk. Reads only.

.DESCRIPTION
    Walks every configured Nextcloud sync folder and writes one line per file
    and directory - path, type, size, mtime - in a format that diffs directly
    against a `find -printf` listing taken from the server's data directory.
    Alongside it goes a report naming the client version, the configured
    folders, the counts, and every file that is a cloud placeholder with no
    local content.

    It exists to answer one question without asking the person running it to
    interpret anything: which files does this machine have, and which does the
    server have. Everything it produces is evidence; nothing it produces is a
    conclusion.

    NOTHING IS WRITTEN INSIDE A SYNC FOLDER. The only calls this script makes
    against the sync tree are directory enumeration and the metadata that
    enumeration already returns - the same thing Explorer does to draw a
    window. No file is opened, no attribute is set, nothing is created, moved
    or deleted. Output goes to the Desktop, and the script refuses to write
    there at all if the Desktop turns out to live inside a folder it is about
    to scan.

.PARAMETER Path
    One or more sync folders to scan. Given this, the client configuration is
    still read for the report but is not used to choose what to walk.

.PARAMETER ConfigFile
    Read the client configuration from this file instead of looking for
    nextcloud.cfg in its usual places.

.PARAMETER OutputDirectory
    Where to leave the result. Defaults to the Desktop.

.PARAMETER NoZip
    Leave the result as a folder instead of a single .zip.

.PARAMETER NoPause
    Do not wait for a keypress at the end. Set this when running from a
    script; leave it off for a human, who otherwise loses the window - and
    with it the line naming the file to send - the moment the walk finishes.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\nextcloud-local-manifest.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\nextcloud-local-manifest.ps1 -Path 'D:\Nextcloud' -NoPause
#>

# THIS FILE IS DELIBERATELY PURE ASCII, and has to stay that way.
#
# Windows PowerShell 5.1 - the one stock on Windows 10 and 11, and the one
# this will be run under - decodes a .ps1 that carries no byte-order mark as
# the system ANSI codepage, not as UTF-8. A single German umlaut in a message
# here would reach the screen as mojibake on exactly the machine this is for.
# The alternatives were a BOM (which the repository's end-of-file hooks and
# every diff then have to carry) or ASCII-only source. ASCII-only won.
#
# This says nothing about FILE NAMES. Those never pass through the parser:
# they arrive from the filesystem as UTF-16 strings and are written out as
# UTF-8 by an encoder chosen explicitly below. Umlauts in the data are fine.
# Umlauts in this file are not.

[CmdletBinding()]
param(
    [string[]] $Path,
    [string] $ConfigFile,
    [string] $OutputDirectory,
    [switch] $NoZip,
    [switch] $NoPause
)

$ErrorActionPreference = 'Stop'

# Windows file attributes that .NET's FileAttributes enum has no names for,
# because they were added for OneDrive-style cloud providers after it was
# frozen. They are the only way to tell a file that is really here from one
# that is a stub the provider will fetch on first read - which is the whole
# reason requirement 5 exists: a dehydrated placeholder reports the real size
# in its metadata and occupies no bytes.
$FILE_ATTRIBUTE_SPARSE_FILE          = 0x00000200
$FILE_ATTRIBUTE_REPARSE_POINT        = 0x00000400
$FILE_ATTRIBUTE_OFFLINE              = 0x00001000
$FILE_ATTRIBUTE_RECALL_ON_OPEN       = 0x00040000
$FILE_ATTRIBUTE_PINNED               = 0x00080000
$FILE_ATTRIBUTE_UNPINNED             = 0x00100000
$FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000

# Any of these means the entry is managed by a cloud filesystem provider.
# A directory carrying one is a normal Files-On-Demand directory and must be
# descended into; a directory carrying a reparse point WITHOUT one is a
# junction or a symlink, which must not be, or a loop back up the tree hangs
# the walk.
$CLOUD_ATTRIBUTES = $FILE_ATTRIBUTE_OFFLINE -bor $FILE_ATTRIBUTE_RECALL_ON_OPEN -bor
                    $FILE_ATTRIBUTE_PINNED -bor $FILE_ATTRIBUTE_UNPINNED -bor
                    $FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS

# Content is not here: opening it would block on a download. OFFLINE is the
# legacy signal, RECALL_ON_DATA_ACCESS the one CfAPI providers - which is what
# a current Nextcloud client uses - actually set.
$DEHYDRATED_ATTRIBUTES = $FILE_ATTRIBUTE_OFFLINE -bor $FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS

# UTF-8 with no BOM. Set-Content -Encoding UTF8 on PowerShell 5.1 emits one,
# and those three bytes land at the head of line 1 of the manifest, where they
# make the first record differ from the server's for reasons that have nothing
# to do with the files.
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

# Latin-1 maps bytes 0x00-0xFF onto U+0000-U+00FF one for one. Decoding a
# UTF-8 byte sequence through it therefore produces a string whose ordinal
# comparison IS a comparison of those bytes - which is what `LC_ALL=C sort`
# does on the server side. Sorting the manifest any other way (PowerShell's
# default Sort-Object is culture-aware, and even an ordinal sort compares
# UTF-16 code units, which reorder astral characters relative to UTF-8) would
# leave the two files sorted differently and the diff meaningless.
$Latin1 = [System.Text.Encoding]::GetEncoding(28591)

$script:Warnings = New-Object System.Collections.Generic.List[string]


function Join-PathSafe {
    <#
        Join-Path, but yielding nothing instead of throwing when the base is
        unset. Every candidate location below is built from an environment
        variable, and a missing one - %ProgramFiles(x86)% on an ARM machine,
        %APPDATA% under a service account - would otherwise end the run with a
        parameter-binding error before anything has been read.
    #>
    param([string] $Base, [string] $Child)
    if ([string]::IsNullOrEmpty($Base)) { return $null }
    return (Join-Path $Base $Child)
}

function Write-Note {
    param([string] $Message, [string] $Colour = 'Gray')
    Write-Host $Message -ForegroundColor $Colour
}

function Add-Warning {
    param([string] $Message)
    $script:Warnings.Add($Message) | Out-Null
    Write-Note "  ! $Message" 'Yellow'
}


function ConvertFrom-QtIniValue {
    <#
        Undo QSettings' escaping of an INI value.

        The client writes nextcloud.cfg through QSettings, which escapes every
        non-ASCII character as \xHHHH and may wrap a value in quotes. A sync
        folder under C:/Users/Kaethe therefore appears as C:/Users/K\xe4the,
        and handing that string to the filesystem finds nothing.
    #>
    param([string] $Value)

    $value = $Value.Trim()
    if ($value.Length -ge 2 -and $value.StartsWith('"') -and $value.EndsWith('"')) {
        $value = $value.Substring(1, $value.Length - 2)
    }

    # Up to four hex digits, which is exactly what QSettings' own reader
    # consumes. The run looks ambiguous - \xdf followed by a literal 'e' would
    # read as U+0DFE - but it is not, because the writer escapes a character
    # that follows an escape and is itself a hex digit. "Gruesse" with the
    # umlaut and eszett comes out as Gr\xfc\xdf\x65, never Gr\xfc\xdfe.
    $value = [regex]::Replace($value, '\\x([0-9a-fA-F]{1,4})', {
        param($match)
        [char][Convert]::ToInt32($match.Groups[1].Value, 16)
    })

    return $value.Replace('\\', '\')
}


function Get-ClientConfiguration {
    <#
        Parse %APPDATA%\Nextcloud\nextcloud.cfg for the sync folders.

        Returns a hashtable: Source, Version, Folders (each with LocalPath,
        TargetPath, VirtualFilesMode, Account), and Raw for the report.

        The interesting keys are flat and deeply prefixed, e.g.
            0\Folders\1\localPath=C:/Users/x/Nextcloud/
            0\Folders\1\virtualFilesMode=wincfapi
        so the parse is: collect every key=value, then pair a localPath with
        its siblings by their common prefix. Matching on the suffix rather
        than on a fixed path is what survives a second account or a folder
        index that is not 1.
    #>
    param([string] $ConfigPath)

    $candidates = @(
        (Join-PathSafe $env:APPDATA 'Nextcloud\nextcloud.cfg'),
        (Join-PathSafe $env:LOCALAPPDATA 'Nextcloud\nextcloud.cfg'),
        (Join-PathSafe $env:APPDATA 'ownCloud\owncloud.cfg')
    )

    $result = @{
        Source           = $null
        Version          = $null
        Folders          = @()
        Raw              = @()
    }

    if ($ConfigPath) { $candidates = @($ConfigPath) }

    $cfg = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
    if (-not $cfg) { return $result }

    $result.Source = $cfg

    $entries = @{}
    $section = ''
    # The file itself is UTF-8; its non-ASCII content is escaped as \xHHHH by
    # QSettings anyway, so this only matters for a hand-edited one.
    foreach ($line in [System.IO.File]::ReadAllLines($cfg, $Utf8NoBom)) {
        $trimmed = $line.Trim()
        if ($trimmed -eq '' -or $trimmed.StartsWith(';') -or $trimmed.StartsWith('#')) { continue }
        if ($trimmed.StartsWith('[') -and $trimmed.EndsWith(']')) {
            $section = $trimmed.Substring(1, $trimmed.Length - 2)
            continue
        }
        $split = $trimmed.IndexOf('=')
        if ($split -lt 1) { continue }
        $key = $trimmed.Substring(0, $split).Trim()
        $value = ConvertFrom-QtIniValue $trimmed.Substring($split + 1)
        $entries["$section/$key"] = $value
        $result.Raw += "$section/$key = $value"
    }

    # [General] version is the client's own version string. [Accounts] also
    # carries a `version`, and it is the settings-schema version - a small
    # integer - so keying on the section matters.
    foreach ($key in 'General/version', 'General/clientVersion') {
        if ($entries.ContainsKey($key)) { $result.Version = $entries[$key]; break }
    }

    $folders = @()
    foreach ($key in $entries.Keys) {
        if ($key -notmatch 'localPath$') { continue }
        $prefix = $key.Substring(0, $key.Length - 'localPath'.Length)

        $local = $entries[$key]
        if ([string]::IsNullOrWhiteSpace($local)) { continue }

        # QSettings stores the path with forward slashes and a trailing one.
        # Converting them back is not cosmetic: the \\?\ prefix applied later
        # turns off the Win32 path parser, forward slashes included, so an
        # extended path containing one resolves to nothing at all.
        $local = $local.Replace('/', '\').TrimEnd('\')

        # Accounts/0\Folders\1\localPath -> Accounts/0\url. Everything up to
        # and including the backslash before "Folders" identifies the account,
        # and it already carries the section name, so nothing is prepended.
        $account = ''
        $split = $prefix.IndexOf('\Folders\')
        if ($split -ge 0) {
            $accountPrefix = $prefix.Substring(0, $split + 1)
            $user = ''
            foreach ($suffix in 'dav_user', 'user') {
                if ($entries.ContainsKey("$accountPrefix$suffix") -and $entries["$accountPrefix$suffix"]) {
                    $user = $entries["$accountPrefix$suffix"]
                    break
                }
            }
            $url = $entries["${accountPrefix}url"]
            $account = (@($user, $url) | Where-Object { $_ }) -join ' @ '
        }

        $folders += [pscustomobject] @{
            LocalPath        = $local
            TargetPath       = $entries["${prefix}targetPath"]
            VirtualFilesMode = $entries["${prefix}virtualFilesMode"]
            Account          = $account
            Key              = $key
        }
    }

    $result.Folders = @($folders | Sort-Object LocalPath -Unique)
    return $result
}


function Get-ClientVersion {
    <#
        The client version, from whichever of three places answers first.

        The configuration file is the least reliable of them - it records the
        version that last wrote the file, which after an upgrade that has not
        yet started the client is the old one - so the installed binary is
        asked first.
    #>
    param($Configuration)

    $exes = @(
        (Join-PathSafe ${env:ProgramFiles} 'Nextcloud\nextcloud.exe'),
        (Join-PathSafe ${env:ProgramFiles(x86)} 'Nextcloud\nextcloud.exe'),
        (Join-PathSafe $env:LOCALAPPDATA 'Programs\Nextcloud\nextcloud.exe')
    )
    foreach ($exe in $exes) {
        if ($exe -and (Test-Path -LiteralPath $exe -PathType Leaf)) {
            try {
                $info = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($exe)
                return "$($info.ProductVersion) ($exe)"
            } catch {
                # Fall through to the next source rather than fail the run:
                # the version is context for the report, not the evidence.
            }
        }
    }

    $uninstall = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Nextcloud',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Nextcloud',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Nextcloud'
    )
    foreach ($key in $uninstall) {
        try {
            $entry = Get-ItemProperty -LiteralPath $key -ErrorAction Stop
            if ($entry.DisplayVersion) { return "$($entry.DisplayVersion) (registry)" }
        } catch {
            # Not installed under that key. Normal; keep looking.
        }
    }

    if ($Configuration.Version) { return "$($Configuration.Version) (nextcloud.cfg)" }
    return 'unknown'
}


function ConvertTo-ExtendedPath {
    <#
        Prefix a path so the Win32 calls under .NET stop enforcing MAX_PATH.

        A Nextcloud tree reaches past 260 characters easily - a few nested
        German folder names will do it - and without this the walk stops with
        "path too long" partway through, which looks exactly like a missing
        directory in the resulting manifest. That is the worst possible
        failure for this tool: silently wrong evidence.
    #>
    param([string] $Literal)

    if ($Literal.StartsWith('\\?\')) { return $Literal }
    if ($Literal.StartsWith('\\'))   { return '\\?\UNC\' + $Literal.Substring(2) }
    return '\\?\' + $Literal
}


# Size on disk, and the only way to tell a fully hydrated file from a partly
# hydrated one. Both report the full logical size, because the provider serves
# that out of the placeholder's metadata; what differs is what the volume has
# actually allocated. A dehydrated placeholder allocates 0, an interrupted
# download allocates some of it, a real local file allocates all of it.
#
# GetCompressedFileSizeW is used rather than opening the file, because opening
# a placeholder is exactly what hydrates it - the thing this whole script must
# never do. This call reads the directory entry and triggers no recall.
#
# Add-Type compiles at run time against the .NET Framework compiler. That is
# present on a stock Windows 10/11, but this script gets one attempt on a
# machine nobody can debug, so a failure degrades to "-" in the on-disk column
# and says so in the report instead of taking the run down.
$Script:SizeOnDiskError = $null
try {
    Add-Type -Namespace NcManifest -Name Native -MemberDefinition @'
[DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
public static extern uint GetCompressedFileSizeW(string lpFileName, out uint lpFileSizeHigh);
'@
} catch {
    $Script:SizeOnDiskError = $_.Exception.Message
}

function Get-SizeOnDisk {
    <#
        Bytes actually allocated for a file, or $null if it cannot be read.

        INVALID_FILE_SIZE (0xFFFFFFFF) in the low word is only an error when
        GetLastError is also non-zero - a genuinely 4 GB-minus-one-byte file
        returns it too. Both are treated as unknown here rather than guessed.
    #>
    param([string] $LiteralPath)

    if ($null -ne $Script:SizeOnDiskError) { return $null }

    try {
        $high = 0
        $low = [NcManifest.Native]::GetCompressedFileSizeW($LiteralPath, [ref] $high)
        if ($low -eq 0xFFFFFFFF) {
            $err = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
            if ($err -ne 0) { return $null }
        }
        return ([long] $high -shl 32) -bor [long] $low
    } catch {
        return $null
    }
}


function Get-ManifestRecords {
    <#
        Walk one sync folder and return its records and statistics.

        Iterative rather than recursive, and using EnumerateFileSystemInfos
        rather than Get-ChildItem, for two reasons that both bite at ten
        thousand files: the enumeration hands back the metadata the directory
        scan already read, so size, mtime and attributes cost no extra call
        each, and a failure in one directory can be recorded and stepped over
        instead of aborting the run.

        READ-ONLY: enumeration and the metadata it returns, and nothing else.
        No stream is opened against anything in here - which also means no
        dehydrated placeholder is ever hydrated by this walk.
    #>
    param([string] $Root)

    $extendedRoot = ConvertTo-ExtendedPath $Root

    $rootInfo = $null
    try {
        $rootInfo = New-Object System.IO.DirectoryInfo($extendedRoot)
        $rootInfo.EnumerateFileSystemInfos() | Select-Object -First 1 | Out-Null
    } catch {
        # .NET Framework before 4.6.2 rejects the \\?\ form outright. Losing
        # long-path support is worse than nothing but far better than not
        # running, and the report says which one happened.
        Add-Warning "long-path prefix rejected for '$Root'; falling back (paths over 260 characters may be missed)"
        $extendedRoot = $Root.TrimEnd('\')
        $rootInfo = New-Object System.IO.DirectoryInfo($extendedRoot)
    }

    $prefixLength = $rootInfo.FullName.TrimEnd('\').Length + 1

    $records      = New-Object System.Collections.Generic.List[string]
    $sortKeys     = New-Object System.Collections.Generic.List[string]
    $cloudEntries = New-Object System.Collections.Generic.List[string]
    $suffixStubs  = New-Object System.Collections.Generic.List[string]
    $unreadable   = New-Object System.Collections.Generic.List[string]

    $stats = @{
        Files             = 0
        Directories       = 0
        Links             = 0
        Bytes             = [long] 0
        CloudManaged      = 0
        Dehydrated        = 0
        DehydratedBytes   = [long] 0
        Partial           = 0
        Pinned            = 0
        Unpinned          = 0
        SizeOnDiskFailed  = 0
        SuffixStubs       = 0
        NonNfcNames       = 0
        Unreadable        = 0
    }

    $stack = New-Object System.Collections.Generic.Stack[System.IO.DirectoryInfo]
    $stack.Push($rootInfo)

    $seen = 0
    $lastReport = [DateTime]::UtcNow

    while ($stack.Count -gt 0) {
        $directory = $stack.Pop()

        $children = $null
        try {
            # Materialised deliberately: EnumerateFileSystemInfos defers its
            # Win32 errors to iteration time, so a lazy pipeline would throw
            # in the middle of the loop below and lose the entries already
            # produced for this directory.
            $children = @($directory.EnumerateFileSystemInfos())
        } catch {
            $relative = if ($directory.FullName.Length -gt $prefixLength) {
                $directory.FullName.Substring($prefixLength).Replace('\', '/')
            } else { '.' }
            $unreadable.Add("$relative : $($_.Exception.Message)") | Out-Null
            $stats.Unreadable++
            continue
        }

        foreach ($child in $children) {
            $seen++

            $full = $child.FullName
            if ($full.Length -le $prefixLength) { continue }
            $relative = $full.Substring($prefixLength).Replace('\', '/')

            $attributes = [int] $child.Attributes
            $isDirectory = ($attributes -band [int][System.IO.FileAttributes]::Directory) -ne 0
            $isReparse   = ($attributes -band $FILE_ATTRIBUTE_REPARSE_POINT) -ne 0
            $isCloud     = ($attributes -band $CLOUD_ATTRIBUTES) -ne 0

            # NFC is what Windows and Linux both normally store; macOS stores
            # NFD, and Nextcloud normalises nothing on the way through. A name
            # that is not NFC will diff against an otherwise identical NFC one
            # with no visible difference at all, so it gets counted here and
            # the comparison recipe knows to fold both sides.
            if (-not $child.Name.IsNormalized([System.Text.NormalizationForm]::FormC)) {
                $stats.NonNfcNames++
            }

            if ($isDirectory) {
                if ($isReparse -and -not $isCloud) {
                    # A junction or a symlink. Recorded, not descended: a
                    # junction pointing at an ancestor turns the walk into a
                    # loop, and its contents are somebody else's tree anyway.
                    $line = "$relative`tl`t-`t-"
                    $stats.Links++
                } else {
                    # Size and mtime are deliberately not recorded for a
                    # directory. Both sides maintain them independently - the
                    # server's PHP writes touch a directory the client never
                    # does, and Linux reports a directory's size as its block
                    # size where Windows reports zero - so recording them
                    # would put a difference on nearly every directory line
                    # and bury the ones that mean something. Existence is what
                    # is being compared.
                    $line = "$relative`td`t-`t-"
                    $stats.Directories++
                    $stack.Push([System.IO.DirectoryInfo] $child)
                }
            } else {
                $size = [long] $child.Length
                # Truncated to the second, not rounded: NTFS keeps 100ns
                # ticks and ext4 keeps nanoseconds, Nextcloud stores and
                # transfers whole seconds, and `find -printf` prints the
                # fraction. Anything finer than a second is noise that would
                # differ on every single file.
                $mtime = $child.LastWriteTimeUtc.ToString('yyyy-MM-ddTHH:mm:ss') + 'Z'
                $line = "$relative`tf`t$size`t$mtime"

                $stats.Files++
                $stats.Bytes += $size

                if ($isCloud) {
                    # Every cloud-managed file, not only the dehydrated ones.
                    # The manifest above cannot carry this: its four columns
                    # have to stay symmetric with `find -printf` on the server
                    # side, and a fifth would break the diff. So cloud state
                    # goes to its own file, where it can have as many columns
                    # as the question needs.
                    #
                    # The logical size in the manifest is what the provider
                    # reports from the placeholder's metadata, so a file with
                    # none of its bytes here still diffs clean against the
                    # server. Which is correct - the bytes are on the server,
                    # so it is not a loss - but it means "is it actually on
                    # this disk" has to be answered somewhere, and this is it.
                    $onDisk = Get-SizeOnDisk $full

                    # Dehydrated is an attribute claim; partial is arithmetic.
                    # A provider sets RECALL_ON_DATA_ACCESS on anything it may
                    # have to fetch, including a file it has already fetched
                    # part of, so the attribute alone cannot separate "nothing
                    # here" from "half here" - only the allocation can.
                    $dehydratedFlag = ($attributes -band $DEHYDRATED_ATTRIBUTES) -ne 0
                    if ($null -eq $onDisk) {
                        # Counted, not shrugged at. Add-Type can succeed and
                        # every call still fail - the DllImport is not resolved
                        # until first use - and a whole column of "-" with no
                        # explanation is the silently-wrong evidence this
                        # script exists to avoid producing.
                        $stats.SizeOnDiskFailed++
                        $state = if ($dehydratedFlag) { 'dehydrated' } else { 'unknown' }
                        $onDiskText = '-'
                    } else {
                        $onDiskText = [string] $onDisk
                        if ($onDisk -eq 0 -and $size -gt 0) {
                            $state = 'dehydrated'
                        } elseif ($onDisk -lt $size) {
                            $state = 'partial'
                            $stats.Partial++
                        } else {
                            $state = 'local'
                        }
                    }

                    # Pin state is the difference between a file that is not
                    # here because somebody chose "Free up space" and one the
                    # client evicted on its own. Without it every dehydrated
                    # file looks equally deliberate.
                    $pin = if (($attributes -band $FILE_ATTRIBUTE_PINNED) -ne 0) {
                        'pinned'
                    } elseif (($attributes -band $FILE_ATTRIBUTE_UNPINNED) -ne 0) {
                        'unpinned'
                    } else {
                        '-'
                    }

                    # The raw word last, so that whatever this misses can still
                    # be recovered from the output instead of needing her
                    # machine a second time.
                    $attrHex = '0x{0:x8}' -f $attributes
                    $cloudEntries.Add("$relative`t$state`t$size`t$onDiskText`t$pin`t$attrHex") | Out-Null

                    $stats.CloudManaged++
                    if ($state -eq 'dehydrated') {
                        $stats.Dehydrated++
                        $stats.DehydratedBytes += $size
                    }
                    if ($pin -eq 'pinned')   { $stats.Pinned++ }
                    if ($pin -eq 'unpinned') { $stats.Unpinned++ }
                }

                if ($relative.EndsWith('.nextcloud') -or $relative.EndsWith('.owncloud')) {
                    # The client's older suffix-based virtual files mode. On
                    # the server this file is named without the suffix, so it
                    # diffs as one missing file plus one extra file unless the
                    # comparison strips it.
                    $suffixStubs.Add($relative) | Out-Null
                    $stats.SuffixStubs++
                }
            }

            $records.Add($line) | Out-Null
            $sortKeys.Add($Latin1.GetString($Utf8NoBom.GetBytes($line))) | Out-Null

            # Throttled: Write-Progress costs more than a directory entry does,
            # and calling it per file roughly halves the walk.
            if (($seen % 200) -eq 0) {
                Write-Progress -Activity "Reading $Root" -Status "$seen entries" -CurrentOperation $relative
            }
            if (([DateTime]::UtcNow - $lastReport).TotalSeconds -ge 5) {
                Write-Note ("  ... {0} entries so far" -f $seen)
                $lastReport = [DateTime]::UtcNow
            }
        }
    }

    Write-Progress -Activity "Reading $Root" -Completed

    # The one sort that matters. Both arrays are reordered together, so the
    # records end up in the byte order of their own UTF-8 encoding - the order
    # `LC_ALL=C sort` produces on the other side.
    $keyArray = $sortKeys.ToArray()
    $lineArray = $records.ToArray()
    [Array]::Sort($keyArray, $lineArray, [StringComparer]::Ordinal)

    $cloudArray = $cloudEntries.ToArray()
    if ($cloudArray.Length -gt 0) {
        $cloudKeys = [string[]] @($cloudArray | ForEach-Object { $Latin1.GetString($Utf8NoBom.GetBytes($_)) })
        [Array]::Sort($cloudKeys, $cloudArray, [StringComparer]::Ordinal)
    }

    return @{
        Lines        = $lineArray
        CloudState   = $cloudArray
        SuffixStubs  = $suffixStubs.ToArray()
        Unreadable   = $unreadable.ToArray()
        Stats        = $stats
    }
}


function Write-TextFile {
    <#
        UTF-8 without a BOM, LF line endings, no trailing surprises.

        Both properties are load-bearing. A BOM corrupts the first record of
        the diff; CRLF makes every single line differ from the server's, which
        is the failure that would make the whole exercise produce a 10000-line
        diff saying nothing.
    #>
    param([string] $FilePath, [string[]] $Lines)

    $stream = New-Object System.IO.StreamWriter($FilePath, $false, $Utf8NoBom)
    try {
        $stream.NewLine = "`n"
        foreach ($line in $Lines) { $stream.WriteLine($line) }
    } finally {
        $stream.Close()
    }
}


function Test-PathInside {
    param([string] $Candidate, [string] $Container)
    $candidate = [System.IO.Path]::GetFullPath($Candidate).TrimEnd('\') + '\'
    $container = [System.IO.Path]::GetFullPath($Container).TrimEnd('\') + '\'
    return $candidate.StartsWith($container, [StringComparison]::OrdinalIgnoreCase)
}


# ---------------------------------------------------------------------------

Write-Note ''
Write-Note 'Nextcloud local manifest - reading only, changing nothing.' 'Cyan'
Write-Note ''

$configuration = Get-ClientConfiguration -ConfigPath $ConfigFile
$clientVersion = Get-ClientVersion $configuration

if ($configuration.Source) {
    Write-Note "Configuration: $($configuration.Source)"
} else {
    Add-Warning 'no nextcloud.cfg found; the sync folder cannot be detected automatically'
}
Write-Note "Client version: $clientVersion"

$roots = @()
if ($Path) {
    $roots = @($Path)
} elseif ($configuration.Folders.Count -gt 0) {
    # Every configured folder, not the first. "Different file sets on the
    # shares" is a statement about more than one folder, and picking one would
    # answer a question nobody asked.
    $roots = @($configuration.Folders | ForEach-Object { $_.LocalPath })
} else {
    $guesses = @(
        (Join-PathSafe $env:USERPROFILE 'Nextcloud'),
        (Join-PathSafe $env:USERPROFILE 'ownCloud')
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Container) }

    if ($guesses.Count -gt 0) {
        $roots = @($guesses)
        Add-Warning "guessed the sync folder from its usual location: $($roots -join ', ')"
    } else {
        Write-Note ''
        Write-Note 'The Nextcloud folder could not be found automatically.' 'Yellow'
        Write-Note 'Open it in Explorer, copy the address from the bar at the top, paste it here.' 'Yellow'
        $answer = Read-Host 'Folder'
        if ($answer) { $roots = @($answer.Trim().Trim('"')) }
    }
}

$roots = @($roots | Where-Object { $_ } | ForEach-Object { $_.TrimEnd('\') } | Select-Object -Unique)

$missing = @($roots | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Container) })
foreach ($gone in $missing) {
    Add-Warning "configured folder does not exist on this machine: $gone"
}
$roots = @($roots | Where-Object { Test-Path -LiteralPath $_ -PathType Container })

if ($roots.Count -eq 0) {
    Write-Note ''
    Write-Note 'No folder to read. Nothing was changed.' 'Red'
    if (-not $NoPause) { Read-Host 'Press Enter to close' | Out-Null }
    exit 1
}

if ($null -ne $Script:SizeOnDiskError) {
    Add-Warning ("size-on-disk unavailable ($Script:SizeOnDiskError); cloud-state files will carry '-' " +
                 'in that column, so a partly fetched file cannot be told from a complete one')
}

Write-Note ''
Write-Note 'Folders to read:'
foreach ($root in $roots) { Write-Note "  $root" 'White' }

# Where the result goes. Desktop first because she has to be able to find it,
# but never inside a folder about to be scanned: writing the manifest into the
# tree the manifest describes would both break the read-only promise and put a
# file on the server that was never meant to be there.
$destination = $OutputDirectory
if (-not $destination) {
    foreach ($candidate in @([Environment]::GetFolderPath('Desktop'), $env:USERPROFILE, [System.IO.Path]::GetTempPath())) {
        if (-not $candidate) { continue }
        $inside = @($roots | Where-Object { Test-PathInside $candidate $_ })
        if ($inside.Count -eq 0) { $destination = $candidate; break }
        Add-Warning "not writing to '$candidate': it is inside the sync folder '$($inside[0])'"
    }
}
if (-not $destination) { throw 'no directory outside the sync folders is available to write to' }

$stamp = (Get-Date).ToString('yyyyMMdd-HHmmss')
$name = "nextcloud-check-$env:COMPUTERNAME-$stamp"

# Assembled under TEMP and then delivered as one item, so the Desktop gains
# exactly one new thing and the final instruction can name exactly one file.
# GetTempPath() rather than %TEMP% directly: it falls back through TMP and the
# Windows directory, so it answers even where the variable is not set.
$staging = Join-Path ([System.IO.Path]::GetTempPath()) $name
New-Item -ItemType Directory -Path $staging -Force | Out-Null

$reportLines = New-Object System.Collections.Generic.List[string]
$reportLines.Add('Nextcloud local manifest') | Out-Null
$reportLines.Add('========================') | Out-Null
$reportLines.Add('') | Out-Null
$reportLines.Add("taken          $((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss'))Z") | Out-Null
$reportLines.Add("computer       $env:COMPUTERNAME") | Out-Null
$reportLines.Add("user           $env:USERNAME") | Out-Null
$reportLines.Add("windows        $([Environment]::OSVersion.VersionString)") | Out-Null
$reportLines.Add("powershell     $($PSVersionTable.PSVersion)") | Out-Null
$reportLines.Add("client version $clientVersion") | Out-Null
$reportLines.Add("configuration  $(if ($configuration.Source) { $configuration.Source } else { 'not found' })") | Out-Null
$reportLines.Add('') | Out-Null

$reportLines.Add('Configured sync folders') | Out-Null
$reportLines.Add('-----------------------') | Out-Null
if ($configuration.Folders.Count -eq 0) {
    $reportLines.Add('  (none read from the configuration)') | Out-Null
} else {
    foreach ($folder in $configuration.Folders) {
        $reportLines.Add("  local        $($folder.LocalPath)") | Out-Null
        $reportLines.Add("  remote       $($folder.TargetPath)") | Out-Null
        $reportLines.Add("  account      $($folder.Account)") | Out-Null
        # "off" or empty means every file is really on disk. Anything else is
        # Files On-Demand and the dehydrated counts below matter.
        $reportLines.Add("  virtual files $(if ($folder.VirtualFilesMode) { $folder.VirtualFilesMode } else { 'off' })") | Out-Null
        $reportLines.Add('') | Out-Null
    }
}

$manifestFiles = @()
$index = 0

foreach ($root in $roots) {
    $index++
    Write-Note ''
    Write-Note "Reading $root" 'Cyan'

    $started = Get-Date
    $result = Get-ManifestRecords $root
    $elapsed = (Get-Date) - $started

    # ASCII and index-prefixed: the file name has to survive being attached to
    # a mail, and the index keeps it unique when two folders share a leaf name.
    $leaf = [regex]::Replace((Split-Path $root -Leaf), '[^A-Za-z0-9._-]', '_')
    $slot = '{0:d2}' -f $index

    $manifestPath = Join-Path $staging "manifest-$slot-$leaf.tsv"
    Write-TextFile $manifestPath $result.Lines
    $manifestFiles += $manifestPath

    if ($result.CloudState.Length -gt 0) {
        # A header is safe here where it would not be in the manifest: this
        # file is read, not diffed, so nothing depends on line 1 being a record.
        $cloudPath = Join-Path $staging "cloud-state-$slot-$leaf.tsv"
        $cloudHeader = @('# path' + "`t" + 'state' + "`t" + 'logical_size' + "`t" + 'size_on_disk' + "`t" + 'pin' + "`t" + 'attributes')
        Write-TextFile $cloudPath ($cloudHeader + $result.CloudState)
        $manifestFiles += $cloudPath
    }

    $stats = $result.Stats
    $reportLines.Add("Folder $slot : $root") | Out-Null
    $reportLines.Add('-' * 60) | Out-Null
    $reportLines.Add("  manifest            $(Split-Path $manifestPath -Leaf)") | Out-Null
    $reportLines.Add("  files               $($stats.Files)") | Out-Null
    $reportLines.Add("  directories         $($stats.Directories)") | Out-Null
    $reportLines.Add("  links/junctions     $($stats.Links)") | Out-Null
    $reportLines.Add("  total bytes         $($stats.Bytes)") | Out-Null
    $reportLines.Add("  cloud-managed       $($stats.CloudManaged)   (Files On-Demand; detail in cloud-state-$slot-$leaf.tsv)") | Out-Null
    $reportLines.Add("  placeholders        $($stats.Dehydrated)   (metadata only, content not on this disk)") | Out-Null
    $reportLines.Add("  placeholder bytes   $($stats.DehydratedBytes)   (counted in total bytes, not present on disk)") | Out-Null
    $reportLines.Add("  partially fetched   $($stats.Partial)   (some bytes here, not all - an interrupted download)") | Out-Null
    if ($stats.SizeOnDiskFailed -gt 0) {
        $reportLines.Add("  size-on-disk failed $($stats.SizeOnDiskFailed)   (of $($stats.CloudManaged) cloud-managed; those rows cannot distinguish partial from complete)") | Out-Null
    }
    $reportLines.Add("  pinned / unpinned   $($stats.Pinned) / $($stats.Unpinned)   (always-keep vs free-up-space, as chosen in Explorer)") | Out-Null
    $reportLines.Add("  .nextcloud stubs    $($stats.SuffixStubs)   (old virtual-files mode; server name has no suffix)") | Out-Null
    $reportLines.Add("  names not NFC       $($stats.NonNfcNames)   (fold both sides before comparing if not 0)") | Out-Null
    $reportLines.Add("  unreadable dirs     $($stats.Unreadable)") | Out-Null
    $reportLines.Add("  time taken          $([int]$elapsed.TotalSeconds)s") | Out-Null
    if ($result.Unreadable.Length -gt 0) {
        $reportLines.Add('  could not be read:') | Out-Null
        foreach ($line in $result.Unreadable) { $reportLines.Add("    $line") | Out-Null }
    }
    if ($result.SuffixStubs.Length -gt 0) {
        $reportLines.Add('  suffix stubs:') | Out-Null
        foreach ($line in $result.SuffixStubs) { $reportLines.Add("    $line") | Out-Null }
    }
    $reportLines.Add('') | Out-Null

    Write-Note ("  {0} files, {1} directories, {2} placeholders of {3} cloud-managed, {4}s" -f `
        $stats.Files, $stats.Directories, $stats.Dehydrated, $stats.CloudManaged,
        [int]$elapsed.TotalSeconds) 'Green'

    # An empty sync folder is possible but is far more often a sign that the
    # walk was pointed at the wrong place, or that the client has never
    # finished its first sync. Either way it must not pass as a quiet zero:
    # an empty manifest compared against the server reads as total data loss.
    if ($stats.Files -eq 0 -and $stats.Directories -eq 0) {
        Add-Warning "'$root' contained nothing at all - check this is really the sync folder"
    }
}

if ($script:Warnings.Count -gt 0) {
    $reportLines.Add('Warnings') | Out-Null
    $reportLines.Add('--------') | Out-Null
    foreach ($warning in $script:Warnings) { $reportLines.Add("  $warning") | Out-Null }
    $reportLines.Add('') | Out-Null
}

$reportLines.Add('Manifest format') | Out-Null
$reportLines.Add('---------------') | Out-Null
$reportLines.Add('  One record per line, tab separated, UTF-8 without a BOM, LF endings,') | Out-Null
$reportLines.Add('  sorted by the UTF-8 bytes of the whole line (what LC_ALL=C sort gives).') | Out-Null
$reportLines.Add('') | Out-Null
$reportLines.Add('    <relative path>  <type>  <size>  <mtime>') | Out-Null
$reportLines.Add('') | Out-Null
$reportLines.Add('  path   relative to the sync folder, forward slashes, no leading ./') | Out-Null
$reportLines.Add('  type   f file, d directory, l junction or symlink') | Out-Null
$reportLines.Add('  size   bytes for a file, - for anything else') | Out-Null
$reportLines.Add('  mtime  UTC, whole seconds, ISO 8601; - for anything but a file') | Out-Null
$reportLines.Add('') | Out-Null
$reportLines.Add('  Nothing in the sync folder was opened, changed, created or deleted.') | Out-Null

$reportPath = Join-Path $staging 'report.txt'
Write-TextFile $reportPath $reportLines.ToArray()

# One item on the Desktop. A zip because it is one attachment rather than
# three, and because a tab-separated file mailed raw tends to arrive mangled.
$delivered = $null
if (-not $NoZip) {
    $zip = Join-Path $destination "$name.zip"
    try {
        Compress-Archive -Path (Join-Path $staging '*') -DestinationPath $zip -Force
        $delivered = $zip
    } catch {
        Add-Warning "could not create the zip ($($_.Exception.Message)); leaving a folder instead"
    }
}
if (-not $delivered) {
    $folder = Join-Path $destination $name
    Copy-Item -LiteralPath $staging -Destination $folder -Recurse -Force
    $delivered = $folder
}

Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue

Write-Note ''
Write-Note '============================================================' 'Green'
Write-Note 'DONE / FERTIG' 'Green'
Write-Note ''
Write-Note 'Send me this file:' 'Green'
Write-Note "  $delivered" 'White'
Write-Note ''
Write-Note 'Bitte schicke mir diese Datei:' 'Green'
Write-Note "  $delivered" 'White'
Write-Note '============================================================' 'Green'
Write-Note ''
Write-Note 'Nothing in your Nextcloud folder was changed.' 'Gray'
Write-Note 'In deinem Nextcloud-Ordner wurde nichts veraendert.' 'Gray'
Write-Note ''

if (-not $NoPause) { Read-Host 'Press Enter to close / Zum Schliessen Enter druecken' | Out-Null }
