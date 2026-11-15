#!/bin/bash
# Linux legs of the 0.7.0 changes: engine/protocol tests that do not need a
# GPU window server beyond surfaceless EGL. Uses the RELEASE engine (only
# one built here; debug would be a cold multi-hour build on /mnt/d).
# Each Driver-spawned test gets its own engine on its own port, so no 7411
# contention; raw-socket tests are skipped (they assume a fresh 7411).
export PATH="/root/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export HOME=/root
R=/mnt/d/Code/2026/Browserr
export BROWSERR_ENGINE="$R/engine/target/release/engine"
cd "$R"

TESTS="test_mcp_logic test_sessions test_useragent test_useragent_mcp test_navigate_quirk test_term test_native_frame test_native_interaction test_frame_pixels test_tab_flow test_login_flow"
failed=""
for t in $TESTS; do
  printf -- "-- %s " "$t"
  timeout 240 python3 "tests/$t.py" > "/tmp/$t.log" 2>&1
  rc=$?
  if [ $rc -eq 0 ]; then echo "OK"; else
    echo "FAIL(rc=$rc)"; tail -6 "/tmp/$t.log"; failed="$failed $t"
  fi
  # leave no engine between tests
  pkill -x engine 2>/dev/null; sleep 0.5
done
echo "=== leftover engine procs ==="
pgrep -x engine || echo "(none)"
[ -n "$failed" ] && { echo "WSL-SUBSET FAILED:$failed"; exit 1; }
echo "WSL-SUBSET-OK ($(( $(echo $TESTS | wc -w) )) tests)"
