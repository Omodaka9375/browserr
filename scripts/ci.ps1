# Local CI runner: same steps as .github/workflows/ci.yml (build both crates,
# run the python test suite against the built binaries).
# Usage: powershell -File scripts\ci.ps1   (CI_WINDOWED=1 to add windowed tests)
# NB: no $ErrorActionPreference='Stop' - pwsh 7.3+ turns native stderr lines
# (cargo warnings) into terminating errors under it. Exit codes checked instead.
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

Write-Host "== build (debug) ==" -ForegroundColor Cyan
cargo build --manifest-path engine\Cargo.toml *> engine-build.log
if ($LASTEXITCODE -ne 0) { Get-Content engine-build.log -Tail 8; exit 1 }
cargo build --manifest-path chrome\Cargo.toml *> chrome-build.log
if ($LASTEXITCODE -ne 0) { Get-Content chrome-build.log -Tail 8; exit 1 }

$env:BROWSERR_ENGINE = "$root\engine\target\debug\engine.exe"

# SDK tests: spawn their own engine via the Driver (ping/pong handshake).
$sdk_tests = @(
    "test_driver", "test_agent", "test_elements", "test_structure",
    "test_watchdog", "test_features", "test_mcp", "test_scroll", "test_bind"
)
# Protocol tests: raw socket, expect an engine already listening on 7411;
# ci.ps1 spawns one for them (a fresh engine per group, like the tests' own
# docs say: never share 7411 between a dying and a booting engine).
$raw_tests = @("test_input", "test_cursor_zoom", "test_close")
# Windowed-chrome tests (attach/hardening/chrome-watchdog) need an
# interactive desktop session; opt in locally with CI_WINDOWED=1.
$win_tests = @()
if ($env:CI_WINDOWED -eq "1") {
    $win_tests = @("test_attach", "test_hardening", "test_chrome_watchdog")
}

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
foreach ($t in $sdk_tests + $win_tests) {
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
Write-Host "ALL GREEN ($($sdk_tests.Count + $raw_tests.Count + $win_tests.Count) tests)" -ForegroundColor Green