"""Adblock verification: with ||example.com/s.js in the filter list, the page
must still load (documents are never blocked) and the engine log must show
"adblock blocked https://example.com/s.js".

Checks the engine stderr log ($TEMP/... via BROWSERR_ENGINE_LOG) for the
blocked line; the engine writes stderr wherever chrome redirected it.
Run: engine.exe --adblock-list=tools/test-filters.txt, then this script.
"""
import json
import socket
import struct
import threading
import time
import os

MAGIC = 0x46524D31
EV_MAGIC = 0x45565431
stop = False
urls = []


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
                e = json.loads(payload)
                if e.get("ev") == "url" and e.get("url"):
                    urls.append(e["url"])
        except socket.timeout:
            continue
        except OSError:
            return


s = socket.create_connection(("127.0.0.1", 7411), timeout=10)
threading.Thread(target=drain, args=(s,), daemon=True).start()
s.sendall(b'{"cmd":"navigate","url":"https://example.com/","tab":0}\n')
time.sleep(10)
stop = True
s.close()

print("urls seen:", urls)
assert any("example.com" in u for u in urls), "page must still load (doc not blocked)"

log = os.environ.get("BROWSERR_ENGINE_LOG", os.path.join(os.environ["TEMP"], "adblock-smoke7.log"))
blocked = "adblock blocked https://example.com/s.js" in open(log, encoding="utf-8", errors="ignore").read()
assert blocked, "engine log must show the blocked subresource"
print("ADBLOCK OK: document loads, subresource blocked")
