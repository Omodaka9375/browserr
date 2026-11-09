# Package a release zip: builds via dist.ps1, then compresses dist/ into
# browserr-<version>-windows-x64.zip at the repo root.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

& (Join-Path $PSScriptRoot "dist.ps1")
if ($LASTEXITCODE -ne 0) { exit 1 }

$dist = Join-Path $root "dist"
$version = "0.1.0"
$zip = Join-Path $root "browserr-$version-windows-x64.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $dist "chrome.exe"), (Join-Path $dist "engine.exe") `
    -DestinationPath $zip -CompressionLevel Optimal
Write-Host "packaged: $zip ($([math]::Round((Get-Item $zip).Length / 1MB, 1)) MB)"
