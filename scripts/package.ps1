# Package a release zip: builds via dist.ps1, then compresses engine.exe,
# the Python SDK, the MCP server, README and LICENSE into
# browserr-<version>-windows-x64.zip at the repo root.
# Version comes from the RELEASE_VERSION env var (CI passes the git tag),
# falling back to the Cargo package version.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

& (Join-Path $PSScriptRoot "dist.ps1")
if ($LASTEXITCODE -ne 0) { exit 1 }

$dist = Join-Path $root "dist"

# Version: env override (release tag), else Cargo.toml package version.
if ($env:RELEASE_VERSION) {
    $version = $env:RELEASE_VERSION
} else {
    $cargoToml = Get-Content (Join-Path $root "engine\Cargo.toml") -Raw
    if ($cargoToml -match 'version\s*=\s*"([^"]+)"') { $version = $Matches[1] }
    else { $version = "0.1.0" }
}
$version = $version.TrimStart("v")

# Release payload: engine + SDK + MCP server + docs.
$payload = @(
    (Join-Path $dist "engine.exe"),
    (Join-Path $root "tools\driver.py"),
    (Join-Path $root "tools\browserr_mcp.py"),
    (Join-Path $root "tools\mcp_wrap.cmd"),
    (Join-Path $root "README.md"),
    (Join-Path $root "LICENSE")
)

$zip = Join-Path $root "browserr-$version-windows-x64.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $payload -DestinationPath $zip -CompressionLevel Optimal
Write-Host "packaged: $zip ($([math]::Round((Get-Item $zip).Length / 1MB, 1)) MB)"