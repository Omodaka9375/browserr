#!/usr/bin/env bash
# Local/CI runner for Linux/macOS: same gate as ci.ps1 (fmt + clippy +
# build + python test suite). GitHub's Linux runners have no GPU but Mesa
# llvmpipe works there, so the FULL render suite runs under xvfb (no
# CI_GPULESS compromise needed - that hack exists for Windows WGL only).
# Usage: bash scripts/ci.sh [--gpuless]
set -u
cd "$(dirname "$0")/.."
root="$PWD"

GPULESS=0
[ "${1:-}" = "--gpuless" ] && GPULESS=1

SDK_TESTS="test_driver test_agent test_elements test_structure test_watchdog test_features test_mcp test_scroll test_bind test_attach test_hardening test_intercept"
RAW_TESTS="test_input test_cursor_zoom test_close"
GPULESS_TESTS="test_bind"

echo "== fmt + clippy gate =="
cargo fmt --manifest-path engine/Cargo.toml -- --check > fmt.log 2>&1 \
    || { tail -10 fmt.log; echo "engine fmt FAILED"; exit 1; }
cargo clippy --manifest-path engine/Cargo.toml -- -D warnings > clippy.log 2>&1 \
    || { grep -E "^(warning|error): " clippy.log | head -5; echo "engine clippy FAILED"; exit 1; }
rm -f fmt.log clippy.log

echo "== build (debug) =="
cargo build --manifest-path engine/Cargo.toml > engine-build.log 2>&1 \
    || { tail -8 engine-build.log; echo "engine build FAILED"; exit 1; }

export BROWSERR_ENGINE="$root/engine/target/debug/engine"
if [ -n "$(command -v xvfb-run)" ]; then
    # Rendering needs X on Linux; xvfb provides a virtual display. Mesa
    # llvmpipe supplies software GL, so no GPU is required.
    XUVB_PREFIX="xvfb-run -a"
fi
run_test() {
    echo "-- $1"
    if [ -n "${XUVB_PREFIX:-}" ]; then
        $XUVB_PREFIX python "tools/$1.py" > "$1.log" 2>&1
    else
        python "tools/$1.py" > "$1.log" 2>&1
    fi
    if [ $? -ne 0 ]; then
        tail -6 "$1.log"
        return 1
    fi
    return 0
}

failed=""
if [ "$GPULESS" -eq 1 ]; then
    for t in $GPULESS_TESTS; do run_test "$t" || failed="$failed $t"; done
else
    for t in $SDK_TESTS; do run_test "$t" || failed="$failed $t"; done
    # Raw-socket tests each expect a fresh engine on 7411 (the engine exits
    # when its single client disconnects).
    for t in $RAW_TESTS; do
        echo "== $t : spawning engine =="
        # Kill any survivor from the previous test first: the engine only
        # exits when its client disconnects, and a straggler holds port 7411
        # (the next engine then dies on "bind failed"). Under xvfb-run, $! is
        # the wrapper's pid, so the per-test kill alone is not reliable.
        pkill -f "$BROWSERR_ENGINE" 2>/dev/null
        sleep 1
        ${XUVB_PREFIX:-} "$BROWSERR_ENGINE" --ephemeral > "engine-$t.log" 2>&1 &
        eng=$!
        sleep 3
        run_test "$t" || failed="$failed $t"
        kill $eng 2>/dev/null
        pkill -f "$BROWSERR_ENGINE" 2>/dev/null
        sleep 1
    done
fi

if [ -n "$failed" ]; then
    echo "FAILED:$failed"
    for lg in engine-*.log; do
        [ -f "$lg" ] && { echo "--- $lg ---"; tail -25 "$lg"; }
    done
    exit 1
fi
rm -f "$root"/*.log
total=$(echo "$SDK_TESTS $RAW_TESTS" | wc -w)
[ "$GPULESS" -eq 1 ] && total=$(echo "$GPULESS_TESTS" | wc -w)
echo "ALL GREEN ($total tests)"