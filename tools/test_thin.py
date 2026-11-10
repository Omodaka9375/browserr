"""Smoke test for the thin (agent) release build: BROWSERR_ENGINE env var
points the driver's spawned engine at the release exe; boot + navigate +
eval + screenshot all must work. Run after `cargo build --release` of engine.
"""

import os as _os
TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
ROOT = _os.path.dirname(TOOLS_DIR)
import functools
import http.server
import threading
import sys
import os

sys.path.insert(0, TOOLS_DIR)
from driver import Driver, save_png

assert "BROWSERR_ENGINE" in os.environ, "set BROWSERR_ENGINE to the release engine.exe"
exe = os.environ["BROWSERR_ENGINE"]
size_mb = os.path.getsize(exe) / 1e6
print(f"engine under test: {exe} ({size_mb:.1f} MB)")

PAGE = """<!doctype html><body style='margin:0'>
<div id=hit style='width:600px;height:600px;background:#f00' onclick="document.title='CLICKED'"></div>
</body>"""

handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=TOOLS_DIR)
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8128), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
with open(TOOLS_DIR + "/thin-test.html", "w") as f:
    f.write(PAGE)

d = Driver(log=f"{ROOT}/engine-thin-test.log")
try:
    d.navigate("http://127.0.0.1:8128/thin-test.html")
    d.wait_load(contains="thin-test.html")
    d.wait_ready()
    assert d.eval_js("2 + 3") == 5
    print("eval OK")
    d.click(300, 300)
    d.wait_for(lambda e: e.get("ev") == "title" and e.get("title") == "CLICKED", timeout=10)
    print("click OK")
    w, h, bgra = d.screenshot()
    assert (w, h) == (1200, 760), (w, h)
    r, g, b = bgra[2], bgra[1], bgra[0]
    assert r > 200 and g < 50 and b < 50, (r, g, b)
    print("screenshot OK")
    print("ALL OK")
finally:
    d.close()
