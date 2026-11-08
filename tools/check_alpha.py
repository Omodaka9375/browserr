"""Grab one frame from the engine and inspect pixel bytes (alpha, channel order)."""
import socket
import struct
from collections import Counter

s = socket.create_connection(("127.0.0.1", 7411), timeout=5)
s.sendall(b'{"cmd":"navigate","url":"https://example.com/"}\n')
s.settimeout(60)

hdr = b""
while len(hdr) < 20:
    chunk = s.recv(20 - len(hdr))
    hdr += chunk
magic, fid, w, h = struct.unpack("<IQII", hdr)
assert magic == 0x46524D31, f"bad magic {magic:#x}"
print(f"frame {fid} {w}x{h}")

n = w * h * 4
buf = bytearray()
while len(buf) < n:
    chunk = s.recv(1 << 20)
    if not chunk:
        raise SystemExit("disconnected")
    buf += chunk

alpha = buf[3::4]
print("alpha counter (first 100k):", Counter(alpha[:400000]).most_common(3))
print("first 8 pixels:", [tuple(buf[i : i + 4]) for i in range(0, 32, 4)])
mid = ((h // 2) * w + w // 2) * 4
print("center pixel:", tuple(buf[mid : mid + 4]))
# opaque fraction
opaque = sum(1 for a in alpha[:1000000] if a == 255)
print(f"opaque: {opaque}/1000000")
