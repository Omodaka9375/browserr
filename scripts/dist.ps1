# Build the engine (release) into dist/.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$cargo = "$env:USERPROFILE\.cargo\bin\cargo.exe"
$dist = Join-Path $root "dist"

& $cargo build --release --manifest-path (Join-Path $root "engine\Cargo.toml")
if ($LASTEXITCODE -ne 0) { exit 1 }

New-Item -ItemType Directory -Force -Path $dist | Out-Null
Copy-Item (Join-Path $root "engine\target\release\engine.exe") $dist -Force
Write-Host "dist ready: $dist"