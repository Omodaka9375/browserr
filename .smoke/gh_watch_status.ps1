# One-line status for the v0.7.0 runs: used by a periodic watch.
$ErrorActionPreference = "Continue"
$repo = "Omodaka9375/browserr"
$sha = "ca62e98c3b02e8fb83173b7b616cb2b2aede89fe"

$inp = "protocol=https`nhost=github.com`n`n"
$out = $inp | git credential fill 2>$null
$pass = $null
foreach ($line in ($out -split "`n")) {
    if ($line -match "^password=(.+)$") { $pass = $Matches[1] }
}
if (-not $pass) { Write-Output "NO_CREDENTIAL"; exit 1 }
$hdr = @{ "User-Agent" = "kai-ci-check"; "Authorization" = "Bearer $pass";
          "Accept" = "application/vnd.github+json" }

try {
    $runs = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/actions/runs?per_page=8" -Headers $hdr
    $line = @()
    foreach ($r in $runs.workflow_runs) {
        if ($r.head_sha -ne $sha) { continue }
        $jobs = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/actions/runs/$($r.id)/jobs?per_page=30" -Headers $hdr
        $js = ($jobs.jobs | ForEach-Object { ($_.name -replace 'test \(|\) \(.*|release \(|\) \(.*') + "=" + ($(if ($_.conclusion) { $_.conclusion } else { $_.status })) }) -join " "
        $line += "$($r.name)#$($r.run_number):$($r.status)/$($r.conclusion) [$js]"
    }
    Write-Output ($line -join " || ")
} catch {
    Write-Output "WATCH-ERROR: $($_.Exception.Message)"
}
