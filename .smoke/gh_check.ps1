# Fetch GitHub Actions job statuses for commit 5fb7b59 using the stored git
# credential (read-only API lookup). The token is used, never printed.
$ErrorActionPreference = "Stop"
$sha = "5fb7b592cc1d38bd7d66655f19feccd5539b060f"
$repo = "Omodaka9375/browserr"

# git credential fill: read stored https credentials for github.com
$inp = "protocol=https`nhost=github.com`n`n"
$out = $inp | git credential fill 2>$null
$pass = $null
foreach ($line in ($out -split "`n")) {
    if ($line -match "^password=(.+)$") { $pass = $Matches[1] }
}
if (-not $pass) { Write-Output "NO_CREDENTIAL"; exit 1 }

$hdr = @{ "User-Agent" = "kai-ci-check"; "Authorization" = "Bearer $pass";
          "Accept" = "application/vnd.github+json" }
$runs = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/actions/runs?per_page=10" -Headers $hdr
foreach ($r in $runs.workflow_runs) {
    if ($r.head_sha -ne $sha) { continue }
    Write-Output "== $($r.name) run $($r.run_number) (id $($r.id)): $($r.status) / $($r.conclusion)"
    $jobs = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/actions/runs/$($r.id)/jobs?per_page=30" -Headers $hdr
    foreach ($j in $jobs.jobs) {
        Write-Output ("  [{0,-8}] {1} ({2})" -f $j.conclusion, $j.name, $j.status)
        foreach ($s in $j.steps) {
            Write-Output ("      {0,-9} {1}" -f $s.conclusion, $s.name)
        }
    }
}
Write-Output "DONE"
