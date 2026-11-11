"""Fail-closed interception test (acts as the chrome).

Arms interception, navigates to a local page, and checks that:
  1. the main-frame document is held for a decision (request event),
  2. "allow" lets the load complete,
  3. a fetch() from the page is held too, "respond" fulfills it with a
     synthetic 200 body the page reads back,
  4. "deny" fulfills a held request with a synthetic 403,
  5. disarming restores normal loads.
"""

import os as _os
TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
ROOT = _os.path.dirname(TOOLS_DIR)
import json
import socket
import struct
import subprocess
import threading
import time
import http.server
import functools
import os
from driver import ENGINE_DEBUG

MAGIC = 0x46524D31
EV_MAGIC = 0x45565431
events = []
stop = False

PAGE = """<!doctype html><html><body>
<div id="out"></div>
<script>
window.__phase = "loaded";
</script>
</body></html>"""

handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=TOOLS_DIR)
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8127), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

with open(TOOLS_DIR + "/intercept-test.html", "w") as f:
    f.write(PAGE)


def drain(s):
    global stop
    while not stop:
        try:
            hdr = b""
            while len(hdr) < 20:
                s.settimeout(0.5)
                chunk = s.recv(20 - len(hdr))
                if not chunk:
                    return
                hdr += chunk
            magic = struct.unpack("<I", hdr[:4])[0]
            a = struct.unpack("<I", hdr[12:16])[0]
            if magic == MAGIC:
                b = struct.unpack("<I", hdr[16:20])[0]
                n = a * b * 4
                got = 0
                while got < n:
                    chunk = s.recv(min(1 << 20, n - got))
                    if not chunk:
                        return
                    got += len(chunk)
            elif magic == EV_MAGIC:
                payload = b""
                while len(payload) < a:
                    chunk = s.recv(a - len(payload))
                    if not chunk:
                        return
                    payload += chunk
                events.append(json.loads(payload))
        except socket.timeout:
            continue
        except OSError:
            return


def cmd(s, o):
    s.sendall((json.dumps(o) + "\n").encode())


def wait_event(pred, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        for e in reversed(events):
            if pred(e):
                return e
        time.sleep(0.05)
    raise SystemExit(f"TIMEOUT waiting for event; recent={[e.get('ev') for e in events[-10:]]}")


def wait_request(url_contains, timeout=10.0):
    return wait_event(
        lambda e: e.get("ev") == "request" and url_contains in e.get("url", ""),
        timeout)


def engine_path():
    return os.environ.get("BROWSERR_ENGINE", ENGINE_DEBUG)


engine = subprocess.Popen([engine_path()],
                          stderr=open(ROOT + "/engine-intercept-test.log", "w"))
s = None
try:
    for _ in range(100):
        try:
            s = socket.create_connection(("127.0.0.1", 7411), timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    if s is None:
        raise SystemExit("could not connect to engine")

    threading.Thread(target=drain, args=(s,), daemon=True).start()
    cmd(s, {"cmd": "resize", "w": 800, "h": 600})

    # 1. Arm; the main-frame document must be held.
    cmd(s, {"cmd": "intercept", "tab": 0, "on": True})
    cmd(s, {"cmd": "navigate", "tab": 0, "url": "http://127.0.0.1:8127/intercept-test.html"})
    req = wait_request("intercept-test.html")
    assert req["main"] is True, f"main-frame flag missing: {req}"
    print("held main-frame document:", req["url"])

    # 2. Allow it; the page loads (poll __phase via eval-with-id).
    cmd(s, {"cmd": "request", "tab": 0, "id": req["id"], "action": "allow"})
    phase = None
    t0 = time.time()
    while time.time() - t0 < 15.0:
        cmd(s, {"cmd": "eval", "tab": 0, "id": 1000, "js": "window.__phase||'none'"})
        try:
            ev = wait_event(lambda e: e.get("ev") == "evalresult"
                            and e.get("id") == 1000, timeout=3.0)
            phase = ev.get("value")
        except SystemExit:
            raise
        if phase == "loaded":
            break
        time.sleep(0.3)
    assert phase == "loaded", f"page never reached scriptable state: {phase}"
    print("allow -> page loaded")

    # 3. A page fetch() is held; respond with a synthetic body.
    js3 = ("fetch('/api/data.json').then(r=>r.text()).then("
           "t=>{document.title='F:'+t}).catch("
           "e=>{document.title='F:ERR'})")
    cmd(s, {"cmd": "eval", "tab": 0, "js": js3})
    req = wait_request("api/data.json")
    cmd(s, {"cmd": "request", "tab": 0, "id": req["id"], "action": "respond",
             "status": 200, "body": "SYNTHETIC", "content_type": "text/plain"})
    ev = wait_event(lambda e: e.get("ev") == "title"
                    and str(e.get("title", "")).startswith("F:"), timeout=15.0)
    body = ev["title"]
    assert "SYNTHETIC" in body, f"synthetic body missing: {body}"
    print("respond -> fetch() saw synthetic body")

    # 4. Deny a held fetch; the page reads a 403.
    js4 = ("fetch('/api/denied.json').then(r=>{document.title='D:'+r.status})"
           ".catch(e=>{document.title='D:ERR'})")
    cmd(s, {"cmd": "eval", "tab": 0, "js": js4})
    req = wait_request("api/denied.json")
    cmd(s, {"cmd": "request", "tab": 0, "id": req["id"], "action": "deny"})
    ev = wait_event(lambda e: e.get("ev") == "title"
                    and str(e.get("title", "")).startswith("D:"), timeout=15.0)
    status = ev["title"]
    assert "403" in status, f"expected synthetic 403, got: {status}"
    print("deny -> fetch() saw 403")

    # 5. Disarm; a reload goes through with no request events.
    n_before = len([e for e in events if e.get("ev") == "request"])
    cmd(s, {"cmd": "intercept", "tab": 0, "on": False})
    cmd(s, {"cmd": "navigate", "tab": 0, "url": "http://127.0.0.1:8127/intercept-test.html"})
    time.sleep(3)
    n_after = len([e for e in events if e.get("ev") == "request"])
    assert n_after == n_before, "request events emitted while disarmed"
    print("disarm -> loads pass through")

    print("ALL OK")
finally:
    stop = True
    if s:
        try:
            s.close()
        except OSError:
            pass
    time.sleep(0.5)
    engine.kill()
