"""Cursor-shape + Ctrl+wheel zoom regression test (acts as the chrome).

Serves a page with a pointer-cursor hit area and a text input. Verifies:
  1. moving over the hit area reports {"ev":"cursor","tab":0,"cursor":"pointer"}
  2. moving over the input reports cursor "text"
  3. moving back to plain body reports cursor "default"
  4. a zoom command changes devicePixelRatio (reported via the title)
"""
import json
import socket
import struct
import subprocess
import sys
import threading
import time
import http.server
import functools
import os

MAGIC = 0x46524D31
EV_MAGIC = 0x45565431
events = []
stop = False

PAGE = """<!doctype html><body style='margin:0'>
<div id=hit style='width:400px;height:300px;background:#f00;cursor:pointer'>pointer zone</div>
<input id=t style='position:fixed;left:0;top:400px;width:300px;height:40px'>
<div style='position:fixed;left:0;top:500px'>plain zone</div>
<script>onresize=()=>{document.title='DPR:'+devicePixelRatio;}</script>
</body>"""

handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=r"D:/Code/2026/Browserr/tools")
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8124), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

with open(r"D:/Code/2026/Browserr/tools/cursor-zoom-test.html", "w") as f:
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
                n = struct.unpack("<I", hdr[16:20])[0]
                if n:
                    pass
                a = struct.unpack("<I", hdr[12:16])[0]
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


def wait_event(pred, timeout=8.0, desc="event"):
    t0 = time.time()
    seen = len(events)
    while time.time() - t0 < timeout:
        for e in events[seen:]:
            if pred(e):
                return e
        # rescan all (cheap) then keep waiting
        for e in events:
            if pred(e):
                return e
        time.sleep(0.1)
    raise SystemExit(f"TIMEOUT waiting for {desc}; events={events[-8:]}")


def engine_path():
    override = os.environ.get("BROWSERR_ENGINE")
    if override:
        return override
    return r"D:/Code/2026/Browserr/engine/target/debug/engine.exe"


engine = subprocess.Popen([engine_path()],
                          stderr=open(r"D:/Code/2026/Browserr/engine-cursor-test.log", "w"))
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
    cmd(s, {"cmd": "navigate", "tab": 0, "url": "http://127.0.0.1:8124/cursor-zoom-test.html"})
    wait_event(lambda e: e.get("ev") == "url" and "cursor-zoom-test" in e.get("url", ""),
               desc="page load")

    # The webview starts with cursor=Pointer, so touch the plain zone first:
    # that change (Pointer -> Default) is what fires the first event.
    for i in range(6):
        cmd(s, {"cmd": "input", "tab": 0, "kind": "move", "x": 150, "y": 520, "button": "none"})
        time.sleep(0.1)
    ev = wait_event(lambda e: e.get("ev") == "cursor" and e.get("cursor") == "default",
                    desc="cursor default")
    print("default cursor ->", ev)

    # 1. pointer over the red hit area
    for i in range(6):
        cmd(s, {"cmd": "input", "tab": 0, "kind": "move", "x": 200, "y": 150, "button": "none"})
        time.sleep(0.1)
    ev = wait_event(lambda e: e.get("ev") == "cursor" and e.get("cursor") == "pointer",
                    desc="cursor pointer")
    print("pointer cursor ->", ev)

    # 2. text cursor over the input
    for i in range(6):
        cmd(s, {"cmd": "input", "tab": 0, "kind": "move", "x": 150, "y": 420, "button": "none"})
        time.sleep(0.1)
    ev = wait_event(lambda e: e.get("ev") == "cursor" and e.get("cursor") == "text",
                    desc="cursor text")
    print("text cursor ->", ev)

    # 4. zoom: page zoom x1.25 doubles devicePixelRatio (hidpi scale 1.0)
    seen_titles = lambda: [e for e in events if e.get("ev") == "title"]
    cmd(s, {"cmd": "zoom", "tab": 0, "zoom": 1.25})
    ev = wait_event(lambda e: e.get("ev") == "title" and str(e.get("title", "")).startswith("DPR:1.25"),
                    timeout=10.0, desc="title DPR:1.25")
    print("zoom ->", ev)

    cmd(s, {"cmd": "zoom", "tab": 0, "zoom": 1.0})
    ev = wait_event(lambda e: e.get("ev") == "title" and e.get("title") == "DPR:1",
                    timeout=10.0, desc="title DPR:1")
    print("zoom reset ->", ev)

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
