# Download the "Upload test logs (always)" artifact for the failed unix job
# and print the tail of its logs. Uses stored git credentials (never printed).
$ErrorActionPreference = "Stop"
$repo = "Omodaka9375/browserr"
$runId = $args[0]
$jobMatch = $args[1]   # substring of the job name, e.g. "ubuntu-24.04,"

$inp = "protocol=https`nhost=github.com`n`n"
$out = $inp | git credential fill 2>$null
$pass = $null
foreach ($line in ($out -split "`n")) {
    if ($line -match "^password=(.+)$") { $pass = $Matches[1] }
}
if (-not $pass) { Write-Output "NO_CREDENTIAL"; exit 1 }
$hdr = @{ "User-Agent" = "kai-ci-check"; "Authorization" = "Bearer $pass";
          "Accept" = "application/vnd.github+json" }

# find the failed job, get its failing step id from the log listing
$jobs = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/actions/runs/$runId/jobs?per_page=30" -Headers $hdr
$job = $jobs.jobs | Where-Object { $_.name -like "*$jobMatch*" } | Select-Object -First 1
Write-Output "JOB: $($job.name) id=$($job.id) conclusion=$($job.conclusion)"

# The unix gate script writes logs to files; the artifact carries them.
# 1) list artifacts of this run
$arts = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/actions/runs/$runId/artifacts?per_page=30" -Headers $hdr
foreach ($a in $arts.artifacts) { Write-Output "artifact: $($a.name) ($($a.size_in_bytes) bytes, id $($a.id))" }
