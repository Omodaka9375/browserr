"""Collect frames for 30s and report whether pixel data alternates between distinct images."""
import socket
import struct
import time
import hashlib

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

deadline = time.time() + 30
seen = {}
order = []
while time.time() < deadline:
    try:
        hdr = recv_exact(s, 20, deadline)
    except socket.timeout:
        break
    magic, fid, w, h = struct.unpack("<IQII", hdr)
    data = recv_exact(s, w * h * 4, deadline)
    digest = hashlib.md5(data).hexdigest()[:8]
    if digest not in seen:
        seen[digest] = 0
        order.append(digest)
    seen[digest] += 1
    print(f"frame {fid}: {digest}")

print("distinct images:", len(seen))
print("counts:", seen)
print("order:", order[:20])
