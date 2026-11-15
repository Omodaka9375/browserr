# Cancel in-flight GitHub Actions runs by id (POST, uses stored git
# credentials; token never printed). Args: run ids.
$ErrorActionPreference = "Continue"
$repo = "Omodaka9375/browserr"

$inp = "protocol=https`nhost=github.com`n`n"
$out = $inp | git credential fill 2>$null
$pass = $null
foreach ($line in ($out -split "`n")) {
    if ($line -match "^password=(.+)$") { $pass = $Matches[1] }
}
if (-not $pass) { Write-Output "NO_CREDENTIAL"; exit 1 }
$hdr = @{ "User-Agent" = "kai-ci-check"; "Authorization" = "Bearer $pass";
          "Accept" = "application/vnd.github+json" }

foreach ($id in $args) {
    try {
        Invoke-RestMethod -Method Post -Uri "https://api.github.com/repos/$repo/actions/runs/$id/cancel" -Headers $hdr | Out-Null
        Write-Output "CANCELLED $id"
    } catch {
        $code = $_.Exception.Response.StatusCode.value__
        if ($code -eq 409) { Write-Output "ALREADY-DONE $id (409 conflict = already completed/cancelled)" }
        else { Write-Output "ERROR $id code=$code $($_.Exception.Message)" }
    }
    Start-Sleep -Seconds 2
    try {
        $r = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/actions/runs/$id" -Headers $hdr
        Write-Output ("  run {0}: status={1} conclusion={2}" -f $id, $r.status, $r.conclusion)
    } catch { Write-Output "  (could not re-read run $id)" }
}
Write-Output "CANCEL-DONE"
