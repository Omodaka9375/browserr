# Print run ids for a head sha. Arg 1 = sha prefix.
$ErrorActionPreference = "Stop"
$repo = "Omodaka9375/browserr"
$sha = $args[0]
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
    if ($r.head_sha -like "$sha*") {
        Write-Output ("{0} #{1} id={2} {3}/{4} run_attempt={5}" -f $r.name, $r.run_number, $r.id, $r.status, $r.conclusion, $r.run_attempt)
        $jobs = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/actions/runs/$($r.id)/jobs?per_page=30" -Headers $hdr
        foreach ($j in $jobs.jobs) {
            Write-Output ("  {0,-10} {1} (id {2})" -f $j.conclusion, $j.name, $j.id)
        }
    }
}
Write-Output "RUNS-DONE"
