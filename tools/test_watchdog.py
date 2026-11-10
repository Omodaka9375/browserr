"""Watchdog test: driver raises EngineDeadError quickly when the engine
process is killed mid-session (no hanging until timeout).

Also verifies the ping path works before the kill.
"""

import os as _os
TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
ROOT = _os.path.dirname(TOOLS_DIR)
import subprocess
import sys
import time

sys.path.insert(0, TOOLS_DIR)
from driver import Driver, EngineDeadError

d = Driver(log=f"{ROOT}/engine-watchdog-test.log")
try:
    d.navigate("https://example.com")
    d.wait_load()
    d.wait_ready()
    print("ping:", d.ping())
    print("alive OK")

    # Kill the engine out from under the driver.
    d.proc.kill()
    t0 = time.time()
    try:
        d.eval_js("1 + 1", timeout=30)
        raise SystemExit("FAIL: no EngineDeadError after engine kill")
    except EngineDeadError as e:
        dt = time.time() - t0
        print(f"EngineDeadError OK ({dt:.2f}s): {e}")
        assert dt < 5, f"took {dt:.1f}s to notice; should be instant"
finally:
    d.close()

print("ALL OK")