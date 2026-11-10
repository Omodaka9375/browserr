# Local CI runner: same steps as .github/workflows/ci.yml (fmt + clippy gate,
# engine build, python test suite).
# Usage: powershell -File scripts\ci.ps1
# NB: no $ErrorActionPreference='Stop' - pwsh 7.3+ turns native stderr lines
# (cargo warnings) into terminating errors under it. Exit codes checked instead.
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

Write-Host "== fmt + clippy gate ==" -ForegroundColor Cyan
cargo fmt --manifest-path engine\Cargo.toml -- --check *> fmt.log
if ($LASTEXITCODE -ne 0) { Get-Content fmt.log -Tail 10; Write-Host "engine fmt FAILED" -ForegroundColor Red; exit 1 }
cargo clippy --manifest-path engine\Cargo.toml -- -D warnings *> clippy-engine.log
if ($LASTEXITCODE -ne 0) { Get-Content clippy-engine.log -Tail 10; Write-Host "engine clippy FAILED" -ForegroundColor Red; exit 1 }
Remove-Item fmt.log, clippy-engine.log -ErrorAction SilentlyContinue

Write-Host "== build (debug) ==" -ForegroundColor Cyan
cargo build --manifest-path engine\Cargo.toml *> engine-build.log
if ($LASTEXITCODE -ne 0) { Get-Content engine-build.log -Tail 8; Write-Host "engine build FAILED" -ForegroundColor Red; exit 1 }

$env:BROWSERR_ENGINE = "$root\engine\target\debug\engine.exe"

# SDK tests: each spawns its own engine via the Driver (ping/pong handshake)
# or attaches to one it spawns with a driver port.
$sdk_tests = @(
    "test_driver", "test_agent", "test_elements", "test_structure",
    "test_watchdog", "test_features", "test_mcp", "test_scroll",
    "test_bind", "test_attach", "test_hardening"
)
# Protocol tests: raw socket, expect a fresh engine listening on 7411.
$raw_tests = @("test_input", "test_cursor_zoom", "test_close")

function Run-Test($t) {
    Write-Host "-- $t" -ForegroundColor Cyan
    python "tools\$t.py" *> "$t.log"
    if ($LASTEXITCODE -ne 0) {
        Get-Content "$t.log" -Tail 6 | Write-Host -ForegroundColor Red
        return $false
    }
    return $true
}

$failed = @()
foreach ($t in $sdk_tests) {
    if (-not (Run-Test $t)) { $failed += $t }
}

# Raw-socket group: each test expects a fresh engine listening on 7411
# (the engine exits when its single client disconnects, so they cannot
# share one).
foreach ($t in $raw_tests) {
    Write-Host "== $t : spawning engine ==" -ForegroundColor Cyan
    $engine = Start-Process -FilePath $env:BROWSERR_ENGINE `
        -ArgumentList "--ephemeral" -PassThru -WindowStyle Hidden `
        -RedirectStandardError "$root\engine-shared.log"
    Start-Sleep 3
    if (-not (Run-Test $t)) { $failed += $t }
    Stop-Process -Id $engine.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep 1
}

if ($failed.Count) {
    Write-Host "FAILED: $($failed -join ', ')" -ForegroundColor Red
    exit 1
}
Remove-Item "$root\*.log" -ErrorAction SilentlyContinue
Write-Host "ALL GREEN ($($sdk_tests.Count + $raw_tests.Count) tests)" -ForegroundColor Green