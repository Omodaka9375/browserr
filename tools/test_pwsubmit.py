"""Save-on-submit verification: a page with a password form auto-submits
after 3s; the engine must capture (username, password) and report a
pwcapture event after the navigation.

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
s.sendall(b'{"cmd":"navigate","url":"http://127.0.0.1:8123/pwtest.html","tab":0}\n')
time.sleep(12)
stop = True
s.close()

caps = [e for e in events if e.get("ev") == "pwcapture"]
print("pwcapture events:", caps)
assert caps, "no capture reported"
d = caps[-1]["data"]
assert d.get("u") == "alice" and d.get("p") == "pw123", f"wrong data: {d}"
assert "127.0.0.1" in d.get("o", ""), f"wrong origin: {d}"
print("PW CAPTURE OK:", d)
