"""Tab-switch scroll persistence test (acts as the chrome).

Loads a tall page, scrolls it, switches to a second tab and back, then reads
scrollY via eval (results flow back as title events).
"""
import json
import socket
import struct
import subprocess
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
<div style='height:5000px;background:linear-gradient(#300,#900)'>tall</div>
</body>"""

handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=r"D:/Code/2026/Browserr/tools")
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8125), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

with open(r"D:/Code/2026/Browserr/tools/scroll-test.html", "w") as f:
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


def title_with(prefix, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        for e in reversed(events):
            if e.get("ev") == "title" and str(e.get("title", "")).startswith(prefix):
                return e["title"]
        time.sleep(0.1)
    raise SystemExit(f"TIMEOUT waiting for {prefix}; titles={[e.get('title') for e in events if e.get('ev')=='title'][-6:]}")


def engine_path():
    return os.environ.get("BROWSERR_ENGINE",
                          r"D:/Code/2026/Browserr/engine/target/debug/engine.exe")


engine = subprocess.Popen([engine_path()],
                          stderr=open(r"D:/Code/2026/Browserr/engine-scroll-test.log", "w"))
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
    cmd(s, {"cmd": "navigate", "tab": 0, "url": "http://127.0.0.1:8125/scroll-test.html"})
    time.sleep(3)

    # Scroll down, confirm the position is non-zero.
    for _ in range(5):
        cmd(s, {"cmd": "wheel", "tab": 0, "dx": 0, "dy": -300, "x": 400, "y": 300})
        time.sleep(0.2)
    cmd(s, {"cmd": "eval", "tab": 0, "js": "document.title='Y1:'+scrollY"})
    y1 = int(title_with("Y1:").split(":")[1])
    print("after scroll:", y1)

    # Switch away and back.
    cmd(s, {"cmd": "new_tab", "tab": 1})
    time.sleep(0.5)
    cmd(s, {"cmd": "activate", "tab": 1})
    time.sleep(1.0)
    cmd(s, {"cmd": "activate", "tab": 0})
    time.sleep(1.0)

    cmd(s, {"cmd": "eval", "tab": 0, "js": "document.title='Y2:'+scrollY"})
    y2 = int(title_with("Y2:").split(":")[1])
    print("after switch back:", y2)

    if y2 == 0:
        raise SystemExit("FAIL: scroll position was reset by the tab switch")
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
