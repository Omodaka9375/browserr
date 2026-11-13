# Build the engine (release) into dist/.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$cargo = "$env:USERPROFILE\.cargo\bin\cargo.exe"
$dist = Join-Path $root "dist"

& $cargo build --release --manifest-path (Join-Path $root "engine\Cargo.toml")
if ($LASTEXITCODE -ne 0) { exit 1 }

New-Item -ItemType Directory -Force -Path $dist | Out-Null
Copy-Item (Join-Path $root "engine\target\release\engine.exe") $dist -Force

# ANGLE DLLs must ship next to engine.exe: servo (no-wgl WebGL path) loads
# libEGL.dll/libGLESv2.dll BY NAME at the first EGL call; without them the
# engine panics on any webgl probe. mozangle builds them into
# <target>/release/build/mozangle-*/out, but WHICH profile dir they land in
# and when is not guaranteed across cargo versions (engine's own build
# script can run before mozangle's finishes on cold builds), so:
#   1. run the built engine once - it self-stages (ensure_angle_dlls in
#      engine/src/main.rs sweeps build/mozangle-*/out next to the binary
#      and copies anything missing);
#   2. sweep the whole release tree as a fallback;
#   3. VERIFY both DLLs exist in dist - fail the packaging loudly rather
#      than shipping an engine that panics on the first webgl page.
$release = Join-Path $root "engine\target\release"
$selfStage = Start-Process -FilePath (Join-Path $release "engine.exe") `
    -ArgumentList "--driver-port=7999" -PassThru
Start-Sleep -Seconds 3
try { Stop-Process -Id $selfStage.Id -Force -ErrorAction SilentlyContinue } catch {}

Get-ChildItem $release -Recurse -Filter "libEGL.dll" -ErrorAction SilentlyContinue |
    Select-Object -First 1 | ForEach-Object { Copy-Item $_.FullName $dist -Force }
Get-ChildItem $release -Recurse -Filter "libGLESv2.dll" -ErrorAction SilentlyContinue |
    Select-Object -First 1 | ForEach-Object { Copy-Item $_.FullName $dist -Force }

foreach ($dll in @("libEGL.dll", "libGLESv2.dll")) {
    $p = Join-Path $dist $dll
    if (-not (Test-Path $p)) {
        Write-Host "::error::dist missing $dll - the ANGLE DLLs must ship" `
            "next to engine.exe or the engine panics on webgl pages"
        exit 1
    }
}
Write-Host "dist ready: $dist"
