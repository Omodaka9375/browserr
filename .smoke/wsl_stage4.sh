#!/bin/bash
# Stage 4: prove the freshly-built Linux engine answers --version WITHOUT
# booting servo (the flag must return before GL init), then run the headless
# self-test (boot + navigate + native PNG/Sixel frame capture) on Linux.
export PATH="/root/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export HOME=/root
R=/mnt/d/Code/2026/Browserr
E="$R/engine/target/release/engine"

echo "=== engine --version (must NOT start listening) ==="
timeout 20 "$E" --version 2>&1 | head -3
echo "rc=${PIPESTATUS[0]}"
sleep 0.5
echo "=== engine procs after probe (want none) ==="
pgrep -x engine && { echo "PROBE BOOTED AN ENGINE (bad)"; pkill -x engine; } || echo "(none) OK"

echo "=== headless self-test: boot + navigate + native PNG/Sixel frames ==="
export BROWSERR_ENGINE="$E"
cd "$R"
timeout 180 python3 term/browserr_term.py --self-test "file://$R/term/native-fixture.html" 2>&1 | tail -12
echo "SELF-TEST rc=${PIPESTATUS[0]}"
pkill -x engine 2>/dev/null; sleep 0.5
pgrep -x engine && echo "leftover engine" || echo "cleanup OK"
echo "WSL-STAGE4-DONE"
