"""v2 verification: tabs, url/title events, per-tab history.

Drains frames in a thread; collects events; drives a scripted session and
prints the event log for checking.
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


def cmd(o):
    s.sendall((json.dumps(o) + "\n").encode())


def wait(sec):
    time.sleep(sec)


cmd({"cmd": "navigate", "url": "https://example.com/", "tab": 0})
wait(8)
cmd({"cmd": "new_tab", "tab": 1})
cmd({"cmd": "activate", "tab": 1})
cmd({"cmd": "navigate", "url": "https://www.wikipedia.org/", "tab": 1})
wait(14)
cmd({"cmd": "activate", "tab": 0})
wait(4)
cmd({"cmd": "activate", "tab": 1})
wait(4)
cmd({"cmd": "close_tab", "tab": 1})
cmd({"cmd": "activate", "tab": 0})
wait(4)
s.close()
print("events seen:")
for e in events:
    print(" ", e)
