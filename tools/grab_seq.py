"""Save frames matching a digest, plus a few others, as PNGs for inspection."""
import socket
import struct
import subprocess
import time

MAGIC = 0x46524D31
URL = "https://dreamworkhq.com/"
TARGETS = {"498d3dc6", "8ad899f6"}


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

deadline = time.time() + 30
saved = set()
others = 0
while time.time() < deadline and (len(saved) < len(TARGETS) or others < 3):
    try:
        hdr = rx(20, deadline)
    except socket.timeout:
        break
    _, fid, w, h = struct.unpack("<IQII", hdr)
    data = rx(w * h * 4, deadline)
    import hashlib
    d = hashlib.md5(data).hexdigest()[:8]
    if d in TARGETS and d not in saved:
        with open(f"tools/recur_{d}.raw", "wb") as f:
            f.write(data)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo",
                        "-pix_fmt", "bgra", "-s", f"{w}x{h}", "-i", f"tools/recur_{d}.raw",
                        "-frames:v", "1", f"tools/recur_{d}.png"], check=True)
        saved.add(d)
        print("saved recurring frame", d)
    elif d not in saved:
        others += 1
        if others <= 3:
            with open(f"tools/other_{others}.raw", "wb") as f:
                f.write(data)
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo",
                            "-pix_fmt", "bgra", "-s", f"{w}x{h}", "-i", f"tools/other_{others}.raw",
                            "-frames:v", "1", f"tools/other_{others}.png"], check=True)
            print("saved other frame", others, d)
