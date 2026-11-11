"""Net evidence smoke test (acts as the chrome).

Loads a page that fetch()es and XHRs, then reads window.__browserr_net
(the document-start user script's ring buffer) via eval.
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
fetch('/net-api.json').then(function(r){return r.text()}).then(function(t){
  document.title='F:'+r.status;
}).catch(function(){document.title='F:ERR'});
try{
  var x=new XMLHttpRequest();
  x.open('GET','/net-xhr.txt');
  x.onload=function(){document.title='X:'+x.status;};
  x.send();
}catch(e){}
</script>
</body></html>"""

handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=TOOLS_DIR)
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8131), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

with open(TOOLS_DIR + "/net-test.html", "w") as f:
    f.write(PAGE)
with open(TOOLS_DIR + "/net-api.json", "w") as f:
    f.write('{"a":1}')
with open(TOOLS_DIR + "/net-xhr.txt", "w") as f:
    f.write("xhrbody")


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


_seq = [800]


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
                          stderr=open(ROOT + "/engine-net-test.log", "w"))
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
    cmd(s, {"cmd": "navigate", "tab": 0, "url": "http://127.0.0.1:8131/net-test.html"})
    time.sleep(4)

    ring = eval_poll("JSON.stringify(window.__browserr_net||[])")
    entries = json.loads(ring) if isinstance(ring, str) else ring
    kinds = {e.get("kind") for e in entries}
    urls = {e.get("url") for e in entries}
    assert "fetch" in kinds, f"no fetch in ring: {entries}"
    assert "xhr" in kinds, f"no xhr in ring: {entries}"
    assert any("net-api.json" in u for u in urls), f"no fetch url: {urls}"
    assert any("net-xhr.txt" in u for u in urls), f"no xhr url: {urls}"
    # completion entries arrive async; poll until done entries show up
    t0 = time.time()
    while time.time() - t0 < 10:
        ring = eval_poll("JSON.stringify(window.__browserr_net||[])")
        entries = json.loads(ring) if isinstance(ring, str) else ring
        kinds = {e.get("kind") for e in entries}
        if "fetch-done" in kinds and "xhr-done" in kinds:
            break
        time.sleep(0.3)
    assert "fetch-done" in kinds, f"no fetch-done: {kinds}"
    assert "xhr-done" in kinds, f"no xhr-done: {kinds}"
    print("ring:", json.dumps(entries, indent=1))
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