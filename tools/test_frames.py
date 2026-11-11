"""Frame tree test (acts as the chrome).

Loads a page with a same-origin iframe, asks for the frame tree via the
frames command, and checks the child's url/name/same_origin plus a
contentDocument eval through the top frame.
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

CHILD = """<!doctype html><html><body>CHILD BODY</body></html>"""

PAGE = """<!doctype html><html><body>
<iframe name="kid" id="kid" src="/frames-child.html"></iframe>
</body></html>"""

handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=TOOLS_DIR)
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8132), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

with open(TOOLS_DIR + "/frames-test.html", "w") as f:
    f.write(PAGE)
with open(TOOLS_DIR + "/frames-child.html", "w") as f:
    f.write(CHILD)


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
    raise SystemExit(f"TIMEOUT; recent={[e.get('ev') for e in events[-10:]]}")


_seq = [900]


def eval_poll(js, timeout=15.0):
    _seq[0] += 1
    rid = _seq[0]
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        cmd(s, {"cmd": "eval", "tab": 0, "id": rid, "js": js})
        try:
            ev = wait_event(lambda e: e.get("ev") == "evalresult"
                            and e.get("id") == rid, timeout=3.0)
            last = ev.get("value")
            if last is not None:
                return last
        except SystemExit:
            raise
        time.sleep(0.3)
    raise SystemExit(f"TIMEOUT polling eval; last={last}")


def engine_path():
    return os.environ.get("BROWSERR_ENGINE", ENGINE_DEBUG)


engine = subprocess.Popen([engine_path()],
                          stderr=open(ROOT + "/engine-frames-test.log", "w"))
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
    cmd(s, {"cmd": "navigate", "tab": 0, "url": "http://127.0.0.1:8132/frames-test.html"})
    time.sleep(4)

    # Frame tree: same-origin child listed with url + name.
    tree = None
    t0 = time.time()
    while time.time() - t0 < 15.0:
        cmd(s, {"cmd": "frames", "tab": 0, "id": 950})
        try:
            ev = wait_event(lambda e: e.get("ev") == "evalresult"
                            and e.get("id") == 950, timeout=3.0)
            raw = ev.get("value")
            tree = json.loads(raw) if isinstance(raw, str) else raw
            if tree and tree.get("children"):
                break
        except SystemExit:
            raise
        time.sleep(0.3)
    assert tree, "no frame tree"
    assert "frames-test.html" in tree.get("url", ""), f"root url: {tree}"
    kids = tree.get("children") or []
    assert any("frames-child.html" in k.get("url", "") for k in kids), \
        f"no child frame: {tree}"
    kid = next(k for k in kids if "frames-child.html" in k.get("url", ""))
    assert kid.get("name") == "kid", f"child name: {kid}"
    assert kid.get("same_origin") is True, f"child same_origin: {kid}"
    print("frame tree:", json.dumps(tree, indent=1))

    # Same-origin contentDocument walk (tier-1 control): read the child body
    # through the parent's frames[] reference.
    body = eval_poll("(function(){try{"
                     "return window.frames[0].document.body.textContent;"
                     "}catch(e){return 'ERR:'+e}})()")
    assert "CHILD BODY" in str(body), f"child body walk: {body}"
    print("contentDocument walk ->", body)

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