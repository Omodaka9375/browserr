"""Input forwarding regression test: clicks, wheel, keys must reach the page.

Serves a page over http.server (data: URLs do not report titles). The page
sets document.title on click and wheel, and mirrors typed text into the title
via an input handler. Events are sent raw over the socket, like the chrome.
"""
import json
import socket
import struct
import threading
import time
import http.server
import functools

MAGIC = 0x46524D31
EV_MAGIC = 0x45565431
stop = False
events = []

PAGE = """<!doctype html><body style='margin:0'>
<div id=hit style='width:600px;height:600px;background:#f00' onclick="document.title='CLICKED'">click me</div>
<input id=t style='position:fixed;left:0;bottom:0' oninput="document.title='TYPED:'+this.value">
<script>window.addEventListener('wheel',()=>{document.title='SCROLLED'},{passive:true});</script>
</body>"""

handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=r"D:/Code/2026/Browserr/tools")
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8123), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

with open(r"D:/Code/2026/Browserr/tools/input-test.html", "w") as f:
    f.write(PAGE)


last_frame = {"w": 0, "h": 0, "red": 0}


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
            b = struct.unpack("<I", hdr[16:20])[0]
            if magic == MAGIC:
                n = a * b * 4
                got = 0
                pixels = bytearray()
                while got < n:
                    chunk = s.recv(min(1 << 20, n - got))
                    if not chunk:
                        return
                    pixels += chunk
                    got += len(chunk)
                # Count red pixels (BGRA: index 2).
                red = sum(1 for i in range(0, len(pixels), 16) if pixels[i + 2] > 200)
                last_frame.update({"w": a, "h": b, "red": red})
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


def title_becomes(needle, timeout=12):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for e in list(events):
            if e.get("ev") == "title" and needle in (e.get("title") or ""):
                return e["title"]
        time.sleep(0.25)
    return None


s = socket.create_connection(("127.0.0.1", 7411), timeout=10)
threading.Thread(target=drain, args=(s,), daemon=True).start()
cmd(s, {"cmd": "frames", "mode": "bytes"})
cmd(s, {"cmd": "navigate", "url": "http://127.0.0.1:8123/input-test.html", "tab": 0})
time.sleep(8)
print("frame:", last_frame)

cmd(s, {"cmd": "input", "tab": 0, "kind": "move", "x": 100, "y": 100, "button": "none"})
cmd(s, {"cmd": "input", "tab": 0, "kind": "down", "x": 100, "y": 100, "button": "left"})
cmd(s, {"cmd": "input", "tab": 0, "kind": "up", "x": 100, "y": 100, "button": "left"})
print("click ->", title_becomes("CLICKED"))

# Diagnostic: read the page's own state (title events prove eval works).
cmd(s, {"cmd": "eval", "tab": 0, "js":
        "document.title='PROBE:'+document.title+'|'+window.innerWidth+'x'+window.innerHeight"})
print("probe ->", title_becomes("PROBE:", 8))

# Does the page's own click handler work at all?
cmd(s, {"cmd": "eval", "tab": 0, "js": "document.getElementById('hit').click()"})
print("synthetic click ->", title_becomes("CLICKED", 6))

cmd(s, {"cmd": "wheel", "tab": 0, "dx": 0, "dy": -120, "x": 100, "y": 100})
print("wheel ->", title_becomes("SCROLLED"))

# Type into the input at the bottom-left of the 1200x760 viewport.
cmd(s, {"cmd": "input", "tab": 0, "kind": "down", "x": 10, "y": 750, "button": "left"})
cmd(s, {"cmd": "input", "tab": 0, "kind": "up", "x": 10, "y": 750, "button": "left"})
time.sleep(0.5)
cmd(s, {"cmd": "eval", "tab": 0, "js": "document.title='FOCUS:'+document.activeElement.id"})
print("focus ->", title_becomes("FOCUS:", 6))
for ch in "hi":
    cmd(s, {"cmd": "key", "tab": 0, "name": "text", "text": ch, "mods": 0, "down": True})
    cmd(s, {"cmd": "key", "tab": 0, "name": "text", "text": ch, "mods": 0, "down": False})
print("typed ->", title_becomes("TYPED:hi", 8))

s.close()
print("titles seen:", [e.get("title") for e in events if e.get("ev") == "title"][-5:])
