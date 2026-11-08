"""Check if a page is animated: count distinct frames over 25s after load."""
import socket
import struct
import sys
import time

MAGIC = 0x46524D31
URL = sys.argv[1] if len(sys.argv) > 1 else "https://dreamworkhq.com/"


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

deadline = time.time() + 25
count = 0
while time.time() < deadline:
    try:
        hdr = rx(20, deadline)
    except socket.timeout:
        break
    _, fid, w, h = struct.unpack("<IQII", hdr)
    rx(w * h * 4, deadline)
    count += 1

print(f"{URL} frames in 25s: {count}")
