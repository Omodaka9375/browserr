# Re-run the failed jobs of an Actions run. Arg 1 = run id.
$ErrorActionPreference = "Stop"
$repo = "Omodaka9375/browserr"
$runId = $args[0]

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
    Invoke-RestMethod -Method Post -Uri "https://api.github.com/repos/$repo/actions/runs/$runId/rerun-failed-jobs" -Headers $hdr | Out-Null
    Write-Output "RERUN-REQUESTED $runId"
} catch {
    $code = $_.Exception.Response.StatusCode.value__
    Write-Output "ERROR code=$code $($_.Exception.Message)"
    try { $sr = New-Object IO.StreamReader($_.Exception.Response.GetResponseStream()); Write-Output $sr.ReadToEnd() } catch {}
}
Start-Sleep -Seconds 5
$r = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/actions/runs/$runId" -Headers $hdr
Write-Output ("run {0}: status={1} conclusion={2} attempt={3}" -f $runId, $r.status, $r.conclusion, $r.run_attempt)
