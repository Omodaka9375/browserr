"""Send one navigate to the engine, then drain frames like the chrome does.

Prints one line per frame received. Keeps the connection open so the engine
does not treat socket close as shutdown.
"""
import socket
import struct
import time

MAGIC = 0x46524D31


def recv_exact(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("disconnected")
        buf += chunk
    return buf


s = socket.create_connection(("127.0.0.1", 7411), timeout=5)
s.sendall(b'{"cmd":"navigate","url":"https://example.com/"}\n')
s.settimeout(90)
deadline = time.time() + 60
count = 0
try:
    while time.time() < deadline:
        hdr = recv_exact(s, 20)
        magic, fid, w, h = struct.unpack("<IQII", hdr)
        if magic != MAGIC:
            raise ValueError(f"bad magic {magic:#x}")
        recv_exact(s, w * h * 4)
        count += 1
        print(f"frame {fid} {w}x{h}", flush=True)
except (ConnectionError, socket.timeout) as e:
    print(f"ended after {count} frames: {e!r}", flush=True)
