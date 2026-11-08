"""Drive back/forward with a frame-draining reader thread, then verify via engine log."""
import socket
import struct
import threading
import time

MAGIC = 0x46524D31
stop = False


def drain(s):
    """Read and discard frames so the engine's send buffer never fills."""
    global stop
    while not stop:
        try:
            hdr = b""
            while len(hdr) < 20:
                s.settimeout(0.5)
                chunk = s.recv(20 - len(hdr))
                if not chunk:
                    return
                hdr += chunk
            magic, fid, w, h = struct.unpack("<IQII", hdr)
            if magic != MAGIC:
                return
            n = w * h * 4
            got = 0
            while got < n:
                chunk = s.recv(min(1 << 20, n - got))
                if not chunk:
                    return
                got += chunk.__len__()
        except socket.timeout:
            continue
        except OSError:
            return


s = socket.create_connection(("127.0.0.1", 7411), timeout=10)
t = threading.Thread(target=drain, args=(s,), daemon=True)
t.start()

s.sendall(b'{"cmd":"navigate","url":"https://example.com/"}\n')
time.sleep(8)
s.sendall(b'{"cmd":"navigate","url":"https://www.wikipedia.org/"}\n')
time.sleep(14)
s.sendall(b'{"cmd":"back"}\n')
time.sleep(8)
s.sendall(b'{"cmd":"forward"}\n')
time.sleep(5)
s.close()
print("done")
