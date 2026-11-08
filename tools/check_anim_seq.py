"""Check frame digest sequence for an animated page: real animation vs A/B alternation."""
import socket
import struct
import time
import hashlib

MAGIC = 0x46524D31
URL = "https://dreamworkhq.com/"


def rx(n, deadline):
    b = b""
    while len(b) < n:
        s.settimeout(max(0.1, deadline - time.time()))
        c = s.recv(n - len(b))
        if not c:
            raise ConnectionError()
        b += c
    return b


s = socket.create_connection(("127.0.0.1", 7411), timeout=10)
s.sendall(b'{"cmd":"resize","w":1280,"h":760}\n')
s.sendall(f'{{"cmd":"navigate","url":"{URL}"}}\n'.encode())

deadline = time.time() + 40
digests = []
while time.time() < deadline:
    try:
        hdr = rx(20, deadline)
    except socket.timeout:
        break
    _, fid, w, h = struct.unpack("<IQII", hdr)
    data = rx(w * h * 4, deadline)
    digests.append(hashlib.md5(data).hexdigest()[:8])

print("frames:", len(digests))
print("sequence:", digests[:30])
distinct = {}
for d in digests:
    distinct[d] = distinct.get(d, 0) + 1
print("distinct:", len(distinct), "top counts:", sorted(distinct.values(), reverse=True)[:10])
