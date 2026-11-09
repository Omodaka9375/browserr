"""Favicon verification: a page with <link rel=icon href=/icon.png> must make
the engine report a favicon event with 16x16 RGBA.

Requires: python -m http.server 8123 --directory tools
"""
import json
import socket
import struct
import threading
import time

MAGIC = 0x46524D31
EV_MAGIC = 0x45565431
stop = False
events = []


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


s = socket.create_connection(("127.0.0.1", 7411), timeout=10)
threading.Thread(target=drain, args=(s,), daemon=True).start()
s.sendall(b'{"cmd":"navigate","url":"http://127.0.0.1:8123/favicon-page.html","tab":0}\n')
time.sleep(12)
stop = True
s.close()

favs = [e for e in events if e.get("ev") == "favicon"]
print("favicon events:", len(favs))
assert favs, "no favicon event"
f = favs[-1]
assert "127.0.0.1" in f.get("origin", ""), f["origin"]
assert f.get("w") == 16 and f.get("h") == 16, (f.get("w"), f.get("h"))
rgba = f["rgba"]
assert len(rgba) == 16 * 16 * 4, len(rgba)
# The icon is a solid red square; check some pixel is red-dominant.
r, g, b = rgba[0], rgba[1], rgba[2]
assert r > 150 and r > g + 50 and r > b + 50, f"not red: {r},{g},{b}"
print("FAVICON OK: 16x16 rgba, red icon decoded")
