"""Navigate, read 3 frames, then check the engine stops sending identical frames."""
import socket
import struct
import time

MAGIC = 0x46524D31


def recv_exact(s, n, deadline):
    buf = b""
    while len(buf) < n:
        s.settimeout(max(0.1, deadline - time.time()))
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("disconnected")
        buf += chunk
    return buf


s = socket.create_connection(("127.0.0.1", 7411), timeout=10)
s.sendall(b'{"cmd":"navigate","url":"https://www.wikipedia.org/"}\n')

deadline = time.time() + 60
for i in range(3):
    hdr = recv_exact(s, 20, deadline)
    magic, fid, w, h = struct.unpack("<IQII", hdr)
    data = recv_exact(s, w * h * 4, deadline)
    print(f"frame {fid} {w}x{h}")

print("waiting 15s for extra frames...")
try:
    extra = s.recv(1 << 16)
    print(f"RECEIVED {len(extra)} more bytes (dedupe failed or content changing)")
except socket.timeout:
    print("no more frames in 15s: dedupe works, stream is quiet")
