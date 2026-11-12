"""Software rendering (--software flag) smoke test.

Runs the real test_driver flow but spawns the engine with --software,
asserting the engine logs the forced CPU context and the driver workflow
still works end to end.
"""

import os as _os
TOOLS_DIR = _os.path.abspath(_os.path.dirname(__file__))
import os
import sys
sys.path.insert(0, TOOLS_DIR)
import subprocess
import threading
import time
import http.server
import functools

from driver import Driver, ENGINE_DEBUG

handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=TOOLS_DIR)
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8141), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

with open(TOOLS_DIR + "/sw-render.html", "w") as f:
    f.write("<html><body><h1>SoftRender OK</h1><p>cpu paint</p></body></html>")

log = os.path.join(TOOLS_DIR, "..", "engine-software-test.log")
d = Driver(engine_path=ENGINE_DEBUG, log=log, engine_args=["--software"])
try:
    d.navigate("http://127.0.0.1:8141/sw-render.html")
    d.wait_load(contains="sw-render.html")
    d.wait_ready()
    title = d.eval_js("document.title")
    w, h, bgra = d.screenshot()
    assert w > 0 and len(bgra) == w * h * 4, "no pixels"
    print("workflow OK:", title, f"{w}x{h}")
finally:
    d.close()

logtxt = open(log).read()
assert "software rendering context (--software)" in logtxt, logtxt[:400]
print("flag OK: engine logged the forced CPU context")
print("ALL OK")
