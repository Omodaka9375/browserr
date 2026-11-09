# Build chrome + engine (release) into dist/ for side-by-side distribution.
# chrome.exe looks for engine.exe next to itself (BROWSERR_ENGINE overrides).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$cargo = "$env:USERPROFILE\.cargo\bin\cargo.exe"
$dist = Join-Path $root "dist"

& $cargo build --release --manifest-path (Join-Path $root "engine\Cargo.toml")
if ($LASTEXITCODE -ne 0) { exit 1 }
& $cargo build --release --manifest-path (Join-Path $root "chrome\Cargo.toml")
if ($LASTEXITCODE -ne 0) { exit 1 }

New-Item -ItemType Directory -Force -Path $dist | Out-Null
Copy-Item (Join-Path $root "engine\target\release\engine.exe") $dist -Force
Copy-Item (Join-Path $root "chrome\target\release\chrome.exe") $dist -Force
Write-Host "dist ready: $dist"
