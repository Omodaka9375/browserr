"""Grab a late frame (after navigation settles) and save it to raw BGRA + PNG."""
import socket
import struct
import subprocess
import sys
import time

MAGIC = 0x46524D31


def recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("disconnected")
        buf += chunk
    return buf


url = sys.argv[1] if len(sys.argv) > 1 else "https://www.wikipedia.org/"
out = sys.argv[2] if len(sys.argv) > 2 else "frame"
skip = int(sys.argv[3]) if len(sys.argv) > 3 else 30

s = socket.create_connection(("127.0.0.1", 7411), timeout=10)
s.sendall(f'{{"cmd":"navigate","url":"{url}"}}\n'.encode())
s.settimeout(120)

last = None
for i in range(skip):
    hdr = recv_exact(s, 20)
    magic, fid, w, h = struct.unpack("<IQII", hdr)
    assert magic == MAGIC, f"bad magic {magic:#x}"
    data = recv_exact(s, w * h * 4)
    last = (w, h, data)
    print(f"got frame {fid} {w}x{h}")

w, h, data = last
with open(f"{out}.raw", "wb") as f:
    f.write(data)
subprocess.run(
    ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgra",
     "-s", f"{w}x{h}", "-i", f"{out}.raw", "-frames:v", "1", f"{out}.png"],
    check=True,
)
print(f"wrote {out}.png")
