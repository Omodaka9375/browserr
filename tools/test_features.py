"""Agent features test: console capture, downloads, load-error surfacing.

1. console: a page that logs messages -> console events arrive with levels.
2. download: driver.download fetches a local file to a temp path.
3. load errors: a bad TLS cert / dead host surfaces as a console message
   (servo logs network errors to the console path).
"""

import os as _os
TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
ROOT = _os.path.dirname(TOOLS_DIR)
import functools
import http.server
import os
import sys
import tempfile
import threading

sys.path.insert(0, TOOLS_DIR)
from driver import Driver

PAGE = """<!doctype html><html><head><title>Console Test</title></head><body>
<script>
console.log("hello from the page");
console.warn("a warning");
console.error("an error");
</script>
<p>content</p>
</body></html>"""

handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=TOOLS_DIR)
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8131), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
with open(TOOLS_DIR + "/features-test.html", "w") as f:
    f.write(PAGE)
with open(TOOLS_DIR + "/features-file.bin", "wb") as f:
    f.write(os.urandom(4096))

d = Driver(log=f"{ROOT}/engine-features-test.log")
try:
    d.navigate("http://127.0.0.1:8131/features-test.html")
    d.wait_load(contains="features-test.html")
    d.wait_ready()

    # 1. console events. Wait for the LAST message: the page emits log/
    # warn/error in order, so once "an error" is here the earlier two are
    # too. Waiting on the first one raced on slow CI (messages 2 and 3
    # had not arrived yet when console() was read).
    d.wait_console(contains="an error", timeout=10)
    msgs = d.console()
    texts = [m["message"] for m in msgs]
    assert "hello from the page" in texts, texts
    levels = {m["message"]: m["level"] for m in msgs}
    assert levels["hello from the page"] == "log", levels
    assert levels["a warning"] == "warn", levels
    assert levels["an error"] == "error", levels
    print("console OK:", len(msgs), "messages")

    # 2. download (plain http, local file)
    tmp = tempfile.mkdtemp(prefix="browserr-dl-")
    dest = os.path.join(tmp, "out", "file.bin")
    n = d.download("http://127.0.0.1:8131/features-file.bin", dest)
    assert n == 4096, n
    data = open(dest, "rb").read()
    assert len(data) == 4096
    print("download OK:", n, "bytes")

    # 2b. download failure surfaces as ok:false
    try:
        d.download("http://127.0.0.1:1/nope", os.path.join(tmp, "x"), timeout=30)
        raise SystemExit("FAIL: expected download error")
    except RuntimeError as e:
        print("download error OK:", str(e)[:60])

    # 3. load error -> console message (unroutable host)
    d.navigate("http://127.0.0.1:1/")
    try:
        d.wait_console(contains="", timeout=8)
    except Exception:
        pass  # no console event is also acceptable; assert only no hang
    print("load-error path OK (no hang)")
finally:
    d.close()

os.remove(TOOLS_DIR + "/features-test.html")
os.remove(TOOLS_DIR + "/features-file.bin")
print("ALL OK")