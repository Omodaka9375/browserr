# Update a Browserr unpack in place to the latest GitHub release.
#
# Runs from inside the unpack (engine.exe next to this script). Downloads
# the platform asset, verifies it against the sha256 digest GitHub reports
# for the asset, then copies the payload over this directory. A running
# engine locks engine.exe on Windows, so it must be stopped first; the
# script refuses to proceed while one is alive.
#
# Usage: update.ps1 [-Version <tag>]  (default: latest release)
#        -WhatIf  reports the target version and asset without touching
#                 the install.
# The backup (temp\browserr-backup-<version>) is removed on success and
# kept on failure so a failed update is recoverable by hand. It must live
# OUTSIDE the install: a backup inside the unpack would be copied into the
# next backup, nesting until paths explode (found by live testing).
#
# Version detection runs the engine with --version; engines older than
# 0.7.0 ignore the flag and start servo instead, so the call runs in a
# job that is killed after a timeout (never leave an engine behind), and
# an "unknown" version is acceptable: the update still proceeds.
[CmdletBinding(SupportsShouldProcess)]
param([string]$Version, [switch]$AllowNoDigest)
$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = "Omodaka9375/browserr"

function Get-EngineVersion([string]$path) {
    $job = Start-Job -ScriptBlock {
        param($p) & $p --version 2>$null
    } -ArgumentList $path
    $out = ""
    if (Wait-Job $job -Timeout 15) { $out = (Receive-Job $job | Out-String) }
    Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
    if ($out -match '(\d+\.\d+\.\d+)') { return $Matches[1] }
    return "unknown"  # pre-0.7 archives have no --version; still updatable
}

# --- locate the install ---------------------------------------------------
$engine = Join-Path $here "engine.exe"
if (-not (Test-Path $engine)) {
    Write-Error "no engine.exe next to this script; run update from inside the unpack"
}
$engineVersion = Get-EngineVersion $engine

# --- a running engine cannot be replaced ----------------------------------
$running = Get-Process engine -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path -ieq $engine }
if ($running) {
    # NOTE the extra parentheses: `"a" + "b" -f $x` binds -f to the SECOND
    # string only, so the message would print a literal {0}.
    Write-Error (("engine is running (pid {0}); stop it first - a running " +
                  "executable cannot be replaced on Windows") -f $running.Id)
}

# --- resolve the release ---------------------------------------------------
$api = if ($Version) {
    "https://api.github.com/repos/$repo/releases/tags/$Version"
} else {
    "https://api.github.com/repos/$repo/releases/latest"
}
$headers = @{ "User-Agent" = "browserr-update" }
$release = Invoke-RestMethod -Uri $api -Headers $headers
$tag = $release.tag_name.TrimStart("v")
if ($tag -eq $engineVersion) {
    Write-Host "already up to date ($engineVersion)"
    exit 0
}

$assetName = "browserr-$tag-windows-x64.zip"
$asset = $release.assets | Where-Object name -eq $assetName
if (-not $asset) {
    Write-Error "release $tag has no asset $assetName"
}

Write-Host "updating $engineVersion -> $tag"
Write-Host "asset: $($asset.name) ($([math]::Round($asset.size / 1MB, 1)) MB)"

# The download URL ships in the same API response as the digest, so it is
# not a MITM defence (TLS is); pin scheme/host and a sane size ceiling so a
# broken/odd response cannot send the download off-site or exhaust memory.
$dl = [uri]$asset.browser_download_url
if ($dl.Scheme -ne "https" -or $dl.Host -notin @("github.com", "objects.githubusercontent.com")) {
    Write-Error "refusing non-GitHub download URL: $($asset.browser_download_url)"
}
if ($asset.size -gt 500MB) {
    Write-Error ("refusing implausibly large asset " +
                 "($([math]::Round($asset.size / 1MB, 1)) MB)")
}

if ($WhatIfPreference) { Write-Host "-WhatIf: stopping before download"; exit 0 }

# --- download and verify ----------------------------------------------------
$tmp = Join-Path $env:TEMP "browserr-update-$tag"
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
try {
    $zip = Join-Path $tmp $assetName
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip `
        -Headers $headers -UserAgent "browserr-update"

    # GitHub's API reports the digest it observed on upload; a mismatch
    # aborts before anything is unpacked. A missing digest is a hard stop
    # too: silently installing an unverified binary defeats the whole
    # point of the check (the digest is the only corruption/asset-swap
    # guard - TLS only covers the transport). Opt in explicitly to skip.
    $expected = ($asset.digest -replace '^sha256:', '').ToLower()
    $actual = (Get-FileHash -Path $zip -Algorithm SHA256).Hash.ToLower()
    if ($expected -and ($actual -ne $expected)) {
        Write-Error "sha256 mismatch: expected $expected got $actual"
    }
    if (-not $expected) {
        if (-not $AllowNoDigest) {
            Write-Error ("release carries no sha256 digest; refusing to " +
                         "install unverified. Pass -AllowNoDigest to " +
                         "override, or re-download the archive by hand.")
        }
        Write-Warning "no digest published; installing unverified (-AllowNoDigest)"
    }

    # --- back up, unpack, replace ------------------------------------------
    $backup = Join-Path $env:TEMP "browserr-backup-$engineVersion"
    if (Test-Path $backup) { Remove-Item $backup -Recurse -Force }
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    Get-ChildItem $here -Force | ForEach-Object {
        Copy-Item $_.FullName (Join-Path $backup $_.Name) -Recurse -Force
    }

    $stage = Join-Path $tmp "unpacked"
    if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
    Expand-Archive -Path $zip -DestinationPath $stage -Force

    # Copy the new payload over the old. The archive is flat; files that
    # exist only in the old version are not removed (none today).
    Get-ChildItem $stage -Force | ForEach-Object {
        Copy-Item $_.FullName $here -Recurse -Force
    }

    # --- verify --------------------------------------------------------------
    $newVersion = Get-EngineVersion $engine
    if ($newVersion -eq "unknown") {
        # The binary we just installed is >= 0.7.0 and always answers
        # --version (it returns before any GL/servo init). Silence here
        # means the copied file is not the expected release: fail closed
        # and keep the backup, matching update.sh.
        Write-Error ("new engine did not answer --version; the copied " +
                     "binary is not the expected release; backup is at $backup")
    } elseif ($newVersion -ne $tag) {
        Write-Error (("updated binary reports {0}, expected {1}; backup is " +
                      "at {2}") -f $newVersion, $tag, $backup)
    }
    # A failed verify keeps the backup (the Write-Error above exits with
    # $ErrorActionPreference = Stop before this line).

    Remove-Item $backup -Recurse -Force
    Write-Host "updated to $tag (backup removed)"
} finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    # The verify step may have started a pre-0.7 engine; do not leave it.
    Get-Process engine -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path -ieq $engine } |
        Stop-Process -Force -ErrorAction SilentlyContinue
}
