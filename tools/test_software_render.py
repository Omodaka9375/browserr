"""Rendering-mode smoke test.

Two cases: the DEFAULT must be CPU rendering (so the engine never fails
on GPU-less machines), and --gpu must opt into the hardware context.
Both are verified by the engine log markers plus a working screenshot.
"""

import os as _os
TOOLS_DIR = _os.path.abspath(_os.path.dirname(__file__))
import os
import sys
sys.path.insert(0, TOOLS_DIR)
import threading
import http.server
import functools

from driver import Driver, ENGINE_DEBUG

handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=TOOLS_DIR)
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8141), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

with open(TOOLS_DIR + "/sw-render.html", "w") as f:
    f.write("<html><body><h1>SoftRender OK</h1><p>cpu paint</p></body></html>")


def run(label, engine_args, expect_marker, forbidden_marker):
    log = os.path.join(TOOLS_DIR, "..", f"engine-render-{label}.log")
    d = Driver(engine_path=ENGINE_DEBUG, log=log, engine_args=engine_args)
    try:
        d.navigate("http://127.0.0.1:8141/sw-render.html")
        d.wait_load(contains="sw-render.html")
        d.wait_ready()
        w, h, bgra = d.screenshot()
        assert w > 0 and len(bgra) == w * h * 4, "no pixels"
    finally:
        d.close()
    logtxt = open(log).read()
    assert expect_marker in logtxt, logtxt[:400]
    assert forbidden_marker not in logtxt, logtxt[:400]
    print(f"{label} OK: {expect_marker}")


# Default: CPU rendering, no GPU attempt at all.
run("default", None,
    "software rendering context (default)",
    "hardware (GPU)")

# --gpu: hardware context on this machine.
run("gpu", ["--gpu"],
    "hardware (GPU) rendering context",
    "software rendering context (default)")

print("ALL OK")
