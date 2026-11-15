# Download the full text log of a job by job id. Arg 1 = job id.
$ErrorActionPreference = "Stop"
$repo = "Omodaka9375/browserr"
$jobId = $args[0]
$dest = "D:/Code/2026/Browserr/.smoke/gh-logs"
if (-not (Test-Path $dest)) { New-Item -ItemType Directory -Path $dest -Force | Out-Null }

$inp = "protocol=https`nhost=github.com`n`n"
$out = $inp | git credential fill 2>$null
$pass = $null
foreach ($line in ($out -split "`n")) {
    if ($line -match "^password=(.+)$") { $pass = $Matches[1] }
}
if (-not $pass) { Write-Output "NO_CREDENTIAL"; exit 1 }
$hdr = @{ "User-Agent" = "kai-ci-check"; "Authorization" = "Bearer $pass";
          "Accept" = "application/vnd.github+json" }

# jobs listing endpoint -> job log redirect URL
$log = "$dest/job-$jobId.log"
Invoke-WebRequest -Uri "https://api.github.com/repos/$repo/actions/jobs/$jobId/logs" -Headers $hdr -OutFile $log -MaximumRedirection 5
Write-Output "saved: $log ($((Get-Item $log).Length) bytes)"
# show the failure region: last FAILED section and its tail
$lines = Get-Content $log
Write-Output "total lines: $($lines.Count)"
$idx = @()
for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i] -match "FAILED|Traceback|AssertionError|Error:") { $idx += $i } }
Write-Output "error-ish lines: $($idx.Count)"
foreach ($i in ($idx | Select-Object -Last 3)) {
    Write-Output "----- context around line $i -----"
    $s = [Math]::Max(0, $i - 20)
    $lines[$s..([Math]::Min($lines.Count - 1, $i + 5))] | ForEach-Object { $_ }
}
Write-Output "JOBLOG-DONE"
