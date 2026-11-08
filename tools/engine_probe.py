"""Test client for the Browserr engine: navigate, grab a frame, report stats."""
import socket, struct, sys, time

HOST, PORT = "127.0.0.1", 7411

def main(url: str) -> None:
    s = socket.create_connection((HOST, PORT), timeout=5)
    s.sendall(f'{{"cmd":"navigate","url":"{url}"}}\n'.encode())
    s.settimeout(30)
    hdr = b""
    while len(hdr) < 20:
        chunk = s.recv(20 - len(hdr))
        if not chunk:
            print("disconnected before frame")
            return
        hdr += chunk
    magic, fid, w, h = struct.unpack("<IQII", hdr)
    assert magic == 0x46524D31, f"bad magic {magic:#x}"
    n = w * h * 4
    buf = bytearray()
    while len(buf) < n:
        buf += s.recv(min(1 << 20, n - len(buf)))
    # stats
    nonblack = sum(1 for i in range(0, n, 4) if buf[i:i+3] != b"\x00\x00\x00")
    uniq = len({bytes(buf[i:i+4]) for i in range(0, n, 4096)})
    print(f"frame id={fid} {w}x{h} nonblack={100*nonblack//(w*h)}% sampled-colors={uniq}")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "https://example.com")
