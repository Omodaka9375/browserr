"""Computer-use test: drive a real chrome.exe window through its driver port.

Spawns chrome.exe --driver-port=7412 (visible window, throwaway profile),
attaches a Driver, then verifies navigation, eval, click, typing, screenshots
and that the UI process is still alive throughout (driver never wedges it).
"""
import functools
import http.server
import subprocess
import threading
import time
import sys

sys.path.insert(0, r"D:/Code/2026/Browserr/tools")
from driver import Driver, save_png

PAGE = """<!doctype html><body style='margin:0'>
<div id=hit style='width:800px;height:600px;background:#f00' onclick="document.title='CLICKED'"></div>
<input id=t style='position:fixed;left:0;top:620px' oninput="document.title='TYPED:'+this.value">
</body>"""

handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=r"D:/Code/2026/Browserr/tools")
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8127), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

with open(r"D:/Code/2026/Browserr/tools/attach-test.html", "w") as f:
    f.write(PAGE)

import os
chrome = os.environ.get("BROWSERR_CHROME",
                        r"D:/Code/2026/Browserr/chrome/target/debug/chrome.exe")
# chrome.exe looks for engine.exe next to itself (only dist/ has that);
# dev-tree runs need the env override.
os.environ.setdefault("BROWSERR_ENGINE",
                       r"D:/Code/2026/Browserr/engine/target/debug/engine.exe")
ui = subprocess.Popen(
    [chrome, "--driver-port=7412", "--profile=drivertest"],
    stderr=open(r"D:/Code/2026/Browserr/chrome-driver-test.log", "w"))

d = None
try:
    d = Driver.attach(7412)
    print("attach OK")
    # The driver port opens before the engine connects; wait until the engine
    # reports something (the initial about:blank / restored url) so early
    # navigates are not dropped.
    d.wait_for(lambda e: e.get("ev") == "url", timeout=30)
    print("engine up")

    d.navigate("http://127.0.0.1:8127/attach-test.html")
    d.wait_load(contains="attach-test.html")
    d.wait_ready()
    print("navigate OK")
    assert ui.poll() is None, "chrome exited during the test"

    assert d.eval_js("2 + 3") == 5
    assert d.eval_js("[1, 'a', true]") == [1, "a", True]
    print("eval OK")
    assert ui.poll() is None

    # Click inside the visible window's page area (below the 40px toolbar).
    d.click(300, 300)
    d.wait_for(lambda e: e.get("ev") == "title" and e.get("title") == "CLICKED", timeout=10)
    print("click OK")
    assert ui.poll() is None

    d.click(50, 630)
    time.sleep(0.5)
    d.type_text("hi")
    time.sleep(0.5)
    got = d.eval_js("document.title")
    assert got == "TYPED:hi", f"title={got!r}"
    print("type OK")
    assert ui.poll() is None

    # Screenshot mirrors what the engine paints for the window.
    w, h, bgra = d.screenshot()
    assert len(bgra) == w * h * 4, (w, h)
    r, g, b = bgra[2], bgra[1], bgra[0]
    assert r > 200 and g < 50 and b < 50, (r, g, b)
    save_png(r"D:/Code/2026/Browserr/tools/attach-test-shot.png", w, h, bgra)
    print("screenshot OK")

    # New tab through the driver, still attached and mirrored.
    d.new_tab(1)
    d.activate(1)
    d.wait_for(lambda e: e.get("ev") == "url" and e.get("tab") == 1
               and "about:blank" in e.get("url", ""), timeout=10)
    print("tabs OK")
    assert ui.poll() is None

    print("ALL OK")
finally:
    if d:
        d.close()   # detach: must NOT kill the UI
    time.sleep(0.5)
    alive = ui.poll() is None
    ui.kill()
    print("ui alive after detach:", alive)
    assert alive, "detach killed the browser window"
