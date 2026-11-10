"""End-to-end test for the automation driver: eval results, input, screenshots.

Serves a page with a red hit area; verifies eval_js returns values, a driver
click reaches the page, and a screenshot contains the red pixels.
"""

import os as _os
TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
ROOT = _os.path.dirname(TOOLS_DIR)
import functools
import http.server
import threading
import time
import sys

sys.path.insert(0, TOOLS_DIR)
from driver import Driver, save_png

PAGE = """<!doctype html><body style='margin:0'>
<div id=hit style='width:600px;height:600px;background:#f00' onclick="document.title='CLICKED'"></div>
<input id=t style='position:fixed;left:0;bottom:0' oninput="document.title='TYPED:'+this.value">
</body>"""

handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=TOOLS_DIR)
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8126), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

with open(TOOLS_DIR + "/driver-test.html", "w") as f:
    f.write(PAGE)

d = Driver(log=f"{ROOT}/engine-driver-test.log")
try:
    d.navigate("http://127.0.0.1:8126/driver-test.html")
    d.wait_load(contains="driver-test.html")
    d.wait_ready()
    print("load OK")

    # eval returns values of every shape
    assert d.eval_js("2 + 3") == 5, d.eval_js("2 + 3")
    assert d.eval_js("'hello ' + 'world'") == "hello world"
    assert d.eval_js("true") is True
    assert d.eval_js("null") is None
    assert d.eval_js("[1, 'a', true]") == [1, "a", True]
    assert d.eval_js("({a: 2, b: 'x'})") == {"a": 2, "b": "x"}
    assert d.eval_js("document.title") == ""
    print("eval OK")

    # driver click reaches the page
    d.click(300, 300)
    d.wait_for(lambda e: e.get("ev") == "title" and e.get("title") == "CLICKED")
    assert d.eval_js("document.title") == "CLICKED"
    print("click OK")

    # typing reaches the input
    d.click(10, 750)
    time.sleep(0.5)  # let the focus take effect before keys (see wait_ready note)
    d.type_text("hi")
    time.sleep(0.3)
    got = d.eval_js("document.title")
    focus = d.eval_js("document.activeElement.id")
    assert got == "TYPED:hi", f"title={got!r} focus={focus!r}"
    print("type OK")

    # screenshot: top-left pixel of the red area is red (BGRA)
    w, h, bgra = d.screenshot()
    assert (w, h) == (1200, 760), (w, h)
    assert len(bgra) == w * h * 4
    r, g, b = bgra[2], bgra[1], bgra[0]
    assert r > 200 and g < 50 and b < 50, (r, g, b)
    save_png(TOOLS_DIR + "/driver-test-shot.png", w, h, bgra)
    print("screenshot OK (red pixel at 10,10)")

    print("ALL OK")
finally:
    d.close()
