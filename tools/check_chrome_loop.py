"""Mimic the chrome: resize then navigate, then report frame cadence for 20s."""
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
s.sendall(b'{"cmd":"resize","w":1280,"h":760}\n')
s.sendall(b'{"cmd":"navigate","url":"https://www.wikipedia.org/"}\n')

deadline = time.time() + 20
count = 0
t_last = time.time()
sizes = []
while time.time() < deadline:
    try:
        hdr = recv_exact(s, 20, deadline)
    except socket.timeout:
        break
    _, fid, w, h = struct.unpack("<IQII", hdr)
    recv_exact(s, w * h * 4, deadline)
    now = time.time()
    count += 1
    sizes.append(f"{w}x{h} +{now - t_last:.2f}s")
    t_last = now
    if count > 60:
        print("loop detected: >60 frames in 20s")
        break

print(f"frames in 20s: {count}")
for line in sizes[:40]:
    print(line)
