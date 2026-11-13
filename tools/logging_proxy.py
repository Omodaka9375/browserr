"""Logging CONNECT proxy with basic-auth enforcement (test fixture).

- Requires Proxy-Authorization: Basic base64(user:pass) matching
  PROXY_USER/PROXY_PASS (default proxyuser:proxypass) on CONNECT.
- 407 + Proxy-Authenticate on failure, so the client's tunnel auth path
  is exercised for real.
- Logs each request line, tunnels bytes, no MITM.
"""
import base64
import socket
import threading
import select
import os

USER = os.environ.get("PROXY_USER", "proxyuser")
PASS = os.environ.get("PROXY_PASS", "proxypass")
EXPECTED = base64.b64encode(f"{USER}:{PASS}".encode()).decode()


def log(msg):
    print(msg, flush=True)


def pipe(a, b):
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 30)
            if not r:
                break
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        pass
    finally:
        try:
            a.close()
        except OSError:
            pass
        try:
            b.close()
        except OSError:
            pass


def handle(client):
    try:
        req = b""
        while b"\r\n\r\n" not in req:
            chunk = client.recv(4096)
            if not chunk:
                client.close()
                return
            req += chunk
        lines = req.split(b"\r\n")
        head = lines[0].decode("latin1", "replace")
        log(f"REQUEST: {head}")
        auth = None
        for ln in lines:
            if ln.lower().startswith(b"proxy-authorization:"):
                auth = ln.split(b":", 1)[1].strip().decode()
                log(f"AUTH: {auth}")
        if not head.startswith("CONNECT"):
            client.sendall(b"HTTP/1.1 501 Not Implemented\r\n\r\n")
            client.close()
            return
        if auth != f"Basic {EXPECTED}":
            log("AUTH: REJECTED -> 407")
            client.sendall(
                b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b"Proxy-Authenticate: Basic realm=\"test\"\r\n\r\n")
            client.close()
            return
        host = head.split()[1]
        try:
            host_only, port = host.rsplit(":", 1)
        except ValueError:
            host_only, port = host, "443"
        upstream = socket.create_connection((host_only, int(port)), timeout=10)
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        log(f"TUNNEL-OPEN: {host}")
        pipe(client, upstream)
        log(f"TUNNEL-CLOSE: {host}")
    except OSError as e:
        log(f"ERROR: {e}")
        try:
            client.close()
        except OSError:
            pass


srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 8888))
srv.listen(16)
log(f"LISTENING 8888 (auth required: {USER}:{PASS})")
while True:
    c, _ = srv.accept()
    threading.Thread(target=handle, args=(c,), daemon=True).start()
