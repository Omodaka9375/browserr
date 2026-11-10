"""Computer-use test: attach to a running engine through its driver port.

Spawns engine.exe --driver-port=7412 (the engine accepts driver clients on a
second port; the main client slot is free for this driver in the test), then
verifies attach + auth, navigation, eval, click, typing, screenshots, and
that the engine stays alive after the driver detaches (socket close on the
driver port must NOT exit the engine; only the main client's disconnect does).
"""

import os as _os
TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
ROOT = _os.path.dirname(TOOLS_DIR)
import functools
import http.server
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, TOOLS_DIR)
from driver import Driver, save_png, ENGINE_DEBUG

PAGE = """<!doctype html><body style='margin:0'>
<div id=hit style='width:800px;height:600px;background:#f00' onclick="document.title='CLICKED'"></div>
<input id=t style='position:fixed;left:0;top:620px' oninput="document.title='TYPED:'+this.value">
</body>"""

handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=TOOLS_DIR)
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8127), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

with open(TOOLS_DIR + "/attach-test.html", "w") as f:
    f.write(PAGE)

engine = os.environ.get(
    "BROWSERR_ENGINE",
    ENGINE_DEBUG)

# The engine needs its MAIN client connected before the driver port opens
# (the listener starts after the first accept). Connect a raw client first.
main = subprocess.Popen(
    [engine, "--ephemeral", "--driver-port=7412", "--driver-token=secret"],
    stdout=subprocess.DEVNULL,
    stderr=open(ROOT + "/engine-attach-test.log", "w"))

# The engine's main port must be connected before the driver port opens
# (the listener starts after the first accept). Hold a raw main connection
# for the test's lifetime; the driver attaches on the second port.
main_sock = None
for _ in range(100):
    try:
        import socket
        main_sock = socket.create_connection(("127.0.0.1", 7411), timeout=1)
        break
    except OSError:
        time.sleep(0.1)
assert main_sock is not None, "engine main port never opened"

d = None
try:
    # The driver port opens once the engine's main slot is taken; retry.
    d = None
    for _ in range(50):
        try:
            d = Driver.attach(7412, token="secret")
            break
        except ConnectionError:
            time.sleep(0.1)
    print("attach OK")

    d.navigate("http://127.0.0.1:8127/attach-test.html")
    d.wait_load(contains="attach-test.html")
    d.wait_ready()
    print("navigate OK")
    assert main.poll() is None, "engine exited during the test"

    assert d.eval_js("2 + 3") == 5
    assert d.eval_js("[1, 'a', true]") == [1, "a", True]
    print("eval OK")

    d.click(300, 300)
    d.wait_for(lambda e: e.get("ev") == "title" and e.get("title") == "CLICKED",
               timeout=10)
    print("click OK")

    d.click(50, 630)
    time.sleep(0.5)
    d.type_text("hi")
    time.sleep(0.5)
    got = d.eval_js("document.title")
    assert got == "TYPED:hi", f"title={got!r}"
    print("type OK")

    w, h, bgra = d.screenshot()
    assert len(bgra) == w * h * 4, (w, h)
    r, g, b = bgra[2], bgra[1], bgra[0]
    assert r > 200 and g < 50 and b < 50, (r, g, b)
    save_png(TOOLS_DIR + "/attach-test-shot.png", w, h, bgra)
    print("screenshot OK")

    d.new_tab(1)
    d.activate(1)
    print("tabs OK")

    print("ALL OK")
finally:
    if d:
        d.close()   # detach: must NOT kill the engine
    time.sleep(0.5)
    alive = main.poll() is None
    # Closing the main connection is the engine's exit path.
    main_sock.close()
    time.sleep(1.0)
    main.kill()
    print("engine alive after detach:", alive)
    assert alive, "detach killed the engine"

os.remove(TOOLS_DIR + "/attach-test.html")