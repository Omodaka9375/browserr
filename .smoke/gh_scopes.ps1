# Show the OAuth scopes of the stored git credential (headers only, token never printed).
$ErrorActionPreference = "Continue"
$inp = "protocol=https`nhost=github.com`n`n"
$out = $inp | git credential fill 2>$null
$pass = $null
foreach ($line in ($out -split "`n")) {
    if ($line -match "^password=(.+)$") { $pass = $Matches[1] }
}
if (-not $pass) { Write-Output "NO_CREDENTIAL"; exit 1 }
try {
    $resp = Invoke-WebRequest -Uri "https://api.github.com/repos/Omodaka9375/browserr" -Method Head -Headers @{ "Authorization" = "Bearer $pass"; "User-Agent" = "kai-ci-check" }
    Write-Output "scopes: $($resp.Headers['X-OAuth-Scopes'])"
    Write-Output "accepted: $($resp.Headers['X-Accepted-OAuth-Scopes'])"
} catch {
    $resp = $_.Exception.Response
    if ($resp) {
        Write-Output "scopes (from error resp): $($resp.Headers['X-OAuth-Scopes'])"
        Write-Output "accepted: $($resp.Headers['X-Accepted-OAuth-Scopes'])"
        Write-Output "code: $($resp.StatusCode.value__)"
    } else { Write-Output "no response: $($_.Exception.Message)" }
}
