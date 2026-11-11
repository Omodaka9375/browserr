"""File picker test (acts as the chrome).

Loads a page with an <input type=file>, clicks it via the input event, and
answers the resulting filepicker event with a real file path; the page
reads the picked file's name and contents back via eval.
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
<input id="pick" type="file">
<div id="out"></div>
<script>
window.__picked = "none";
document.getElementById('pick').addEventListener('change', function () {
  var f = this.files[0];
  if (!f) { window.__picked = "empty"; return; }
  var r = new FileReader();
  r.onload = function () {
    window.__picked = f.name + ":" + r.result;
  };
  r.readAsText(f);
});
</script>
</body></html>"""

PICKED_PATH = os.path.join(TOOLS_DIR, "filepicker-picked.txt")
with open(PICKED_PATH, "w") as f:
    f.write("PICKED CONTENTS 42")

handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=TOOLS_DIR)
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8130), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

with open(TOOLS_DIR + "/filepicker-test.html", "w") as f:
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


_eval_seq = [770]


def eval_poll(js, timeout=15.0):
    """Poll an eval-with-id until its result is a non-None string.
    A fresh id per call: a stale evalresult from an earlier poll must
    never satisfy this one's predicate."""
    _eval_seq[0] += 1
    rid = _eval_seq[0]
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        cmd(s, {"cmd": "eval", "tab": 0, "id": rid, "js": js})
        try:
            ev = wait_event(lambda e: e.get("ev") == "evalresult"
                            and e.get("id") == rid, timeout=3.0)
            last = ev.get("value")
            if last and str(last) != "none":
                return last
        except SystemExit:
            raise
        time.sleep(0.3)
    raise SystemExit(f"TIMEOUT polling eval; last={last}")


def engine_path():
    return os.environ.get("BROWSERR_ENGINE", ENGINE_DEBUG)


engine = subprocess.Popen([engine_path()],
                          stderr=open(ROOT + "/engine-filepicker-test.log", "w"))
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
    cmd(s, {"cmd": "navigate", "tab": 0, "url": "http://127.0.0.1:8130/filepicker-test.html"})
    time.sleep(3)

    # 1. Clicking the file input raises the picker (held by the engine).
    pt = None
    t0 = time.time()
    while time.time() - t0 < 15.0:
        pt = eval_poll("(function(){var e=document.getElementById('pick');"
                       "if(!e){return null;}var r=e.getBoundingClientRect();"
                       "return {x:r.x+r.width/2,y:r.y+r.height/2};})()")
        if isinstance(pt, dict) and "x" in pt and "y" in pt:
            break
        pt = None
        time.sleep(0.3)
    assert pt, "page never gave a pick rect"
    cmd(s, {"cmd": "input", "tab": 0, "kind": "down", "x": pt["x"], "y": pt["y"],
             "button": "left"})
    cmd(s, {"cmd": "input", "tab": 0, "kind": "up", "x": pt["x"], "y": pt["y"],
             "button": "left"})
    pk = wait_event(lambda e: e.get("ev") == "filepicker", timeout=10.0)
    print("filepicker event:", pk)

    # 2. Answer it with the real file path; the change handler fires and
    #    the FileReader reads the contents back.
    cmd(s, {"cmd": "files", "tab": 0, "id": pk["id"],
             "paths": [PICKED_PATH.replace("\\", "/")]})
    picked = eval_poll("window.__picked")
    assert "filepicker-picked.txt:PICKED CONTENTS 42" in str(picked), \
        f"unexpected picked value: {picked}"
    print("picked ->", picked)

    # 3. A cancelled picker (empty paths) makes no selection: reload so the
    #    input starts empty (a dismiss keeps the previous FileList), then
    #    click, answer with [], and confirm files.length stays 0.
    cmd(s, {"cmd": "navigate", "tab": 0, "url": "http://127.0.0.1:8130/filepicker-test.html?fresh=1"})
    time.sleep(3)
    pt = None
    t0 = time.time()
    while time.time() - t0 < 15.0:
        pt = eval_poll("(function(){var e=document.getElementById('pick');"
                        "if(!e){return null;}var r=e.getBoundingClientRect();"
                        "return {x:r.x+r.width/2,y:r.y+r.height/2};})()")
        if isinstance(pt, dict) and "x" in pt and "y" in pt:
            break
        pt = None
        time.sleep(0.3)
    assert pt, "fresh page never gave a pick rect"
    cmd(s, {"cmd": "input", "tab": 0, "kind": "down", "x": pt["x"], "y": pt["y"],
             "button": "left"})
    cmd(s, {"cmd": "input", "tab": 0, "kind": "up", "x": pt["x"], "y": pt["y"],
             "button": "left"})
    pk2 = wait_event(lambda e: e.get("ev") == "filepicker"
                      and e.get("id", 0) > pk["id"], timeout=10.0)
    cmd(s, {"cmd": "files", "tab": 0, "id": pk2["id"], "paths": []})
    cancelled = eval_poll(
        "(document.getElementById('pick').files.length)+':'+window.__picked")
    assert cancelled.startswith("0:"), f"cancel left files: {cancelled}"
    print("cancel ->", cancelled)

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