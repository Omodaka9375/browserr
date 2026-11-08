"""Navigate, wait for load to settle, then measure steady-state frame rate for 30s."""
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
# drain load frames
deadline = time.time() + 45
try:
    while time.time() < deadline:
        hdr = recv_exact(s, 20, deadline)
        _, fid, w, h = struct.unpack("<IQII", hdr)
        recv_exact(s, w * h * 4, deadline)
        print(f"load frame {fid}")
except socket.timeout:
    pass

print("--- steady state: collecting 30s ---")
deadline = time.time() + 30
count = 0
digests = []
while time.time() < deadline:
    try:
        hdr = recv_exact(s, 20, deadline)
    except socket.timeout:
        break
    _, fid, w, h = struct.unpack("<IQII", hdr)
    data = recv_exact(s, w * h * 4, deadline)
    count += 1
    digests.append(hashlib.md5(data).hexdigest()[:8])

print(f"steady frames in 30s: {count}")
print("digests:", digests)
