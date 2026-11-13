# Build the engine (release) into dist/.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$cargo = "$env:USERPROFILE\.cargo\bin\cargo.exe"
$dist = Join-Path $root "dist"

& $cargo build --release --manifest-path (Join-Path $root "engine\Cargo.toml")
if ($LASTEXITCODE -ne 0) { exit 1 }

New-Item -ItemType Directory -Force -Path $dist | Out-Null
Copy-Item (Join-Path $root "engine\target\release\engine.exe") $dist -Force
# ANGLE DLLs must ship next to engine.exe (the no-wgl WebGL path loads
# libEGL/libGLESv2 by name at runtime). They may be missing from the
# profile dir on a cold build (engine's build script can run before
# mozangle finishes), so sweep mozangle's build out-dirs directly - the
# authoritative source after any successful compile.
$staged = 0
Get-ChildItem (Join-Path $root "engine\target\release\build") -Directory -Filter "mozangle-*" -ErrorAction SilentlyContinue | ForEach-Object {
    Get-ChildItem (Join-Path $_.FullName "out") -Filter "*.dll" -ErrorAction SilentlyContinue | ForEach-Object {
        Copy-Item $_.FullName $dist -Force
        $staged++
    }
}
if ($staged -eq 0) {
    # Already staged next to the binary by engine/build.rs: copy those.
    Copy-Item (Join-Path $root "engine\target\release\libEGL.dll") $dist -Force -ErrorAction SilentlyContinue
    Copy-Item (Join-Path $root "engine\target\release\libGLESv2.dll") $dist -Force -ErrorAction SilentlyContinue
}
Write-Host "dist ready: $dist"
