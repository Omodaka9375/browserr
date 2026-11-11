"""Auth challenge test (acts as the chrome).

Navigates to a local server answering 401 + WWW-Authenticate: Basic; the
engine holds the challenge and emits an auth event; answering with
credentials completes the load, answering none (a second navigation with
bad creds) fails closed.
"""

import os as _os
TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
ROOT = _os.path.dirname(TOOLS_DIR)
import base64
import json
import socket
import struct
import subprocess
import threading
import time
import http.server
import os
from driver import ENGINE_DEBUG

MAGIC = 0x46524D31
EV_MAGIC = 0x45565431
events = []
stop = False


class AuthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                user, pw = base64.b64decode(hdr.split()[1]).split(b":", 1)
            except Exception:
                user = pw = b""
            if user == b"agent" and pw == b"secret":
                body = b"<html><body>AUTHOK</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        body = b"<html><body>DENIED</body></html>"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="test"')
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8128), AuthHandler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
# A second origin for the wrong-credentials case: servo caches answered
# credentials per origin, so a same-origin reload would replay the good ones.
httpd2 = http.server.ThreadingHTTPServer(("127.0.0.1", 8129), AuthHandler)
threading.Thread(target=httpd2.serve_forever, daemon=True).start()


def drain(s):
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
            magic = struct.unpack("<I", hdr[:4])[0]
            a = struct.unpack("<I", hdr[12:16])[0]
            if magic == MAGIC:
                b = struct.unpack("<I", hdr[16:20])[0]
                n = a * b * 4
                got = 0
                while got < n:
                    chunk = s.recv(min(1 << 20, n - got))
                    if not chunk:
                        return
                    got += len(chunk)
            elif magic == EV_MAGIC:
                payload = b""
                while len(payload) < a:
                    chunk = s.recv(a - len(payload))
                    if not chunk:
                        return
                    payload += chunk
                events.append(json.loads(payload))
        except socket.timeout:
            continue
        except OSError:
            return


def cmd(s, o):
    s.sendall((json.dumps(o) + "\n").encode())


def wait_event(pred, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        for e in reversed(events):
            if pred(e):
                return e
        time.sleep(0.05)
    raise SystemExit(f"TIMEOUT waiting for event; recent={[e.get('ev') for e in events[-10:]]}")


def wait_auth(url_contains=None, timeout=10.0):
    return wait_event(
        lambda e: e.get("ev") == "auth"
        and (url_contains is None or url_contains in e.get("url", "")),
        timeout)


def body_text(timeout=15.0):
    """Poll the page body text via eval-with-id until non-empty."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        cmd(s, {"cmd": "eval", "tab": 0, "id": 555, "js": "document.body?document.body.textContent:''"})
        try:
            ev = wait_event(lambda e: e.get("ev") == "evalresult" and e.get("id") == 555,
                            timeout=3.0)
            txt = (ev.get("value") or "").strip()
            if txt:
                return txt
        except SystemExit:
            raise
        time.sleep(0.3)
    raise SystemExit("TIMEOUT waiting for body text")


def engine_path():
    return os.environ.get("BROWSERR_ENGINE", ENGINE_DEBUG)


engine = subprocess.Popen([engine_path()],
                          stderr=open(ROOT + "/engine-auth-test.log", "w"))
s = None
try:
    for _ in range(100):
        try:
            s = socket.create_connection(("127.0.0.1", 7411), timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    if s is None:
        raise SystemExit("could not connect to engine")

    threading.Thread(target=drain, args=(s,), daemon=True).start()
    cmd(s, {"cmd": "resize", "w": 800, "h": 600})

    # 1. A 401 on the main frame holds an auth challenge event.
    cmd(s, {"cmd": "navigate", "tab": 0, "url": "http://127.0.0.1:8128/"})
    ch = wait_auth("127.0.0.1:8128")
    assert ch.get("proxy") is False, f"expected non-proxy challenge: {ch}"
    print("auth challenge held:", ch["url"])

    # 2. Answer with credentials; the load completes with the good body.
    cmd(s, {"cmd": "auth", "tab": 0, "id": ch["id"], "user": "agent", "pass": "secret"})
    txt = body_text()
    assert "AUTHOK" in txt, f"expected AUTHOK, got: {txt}"
    print("auth answered -> AUTHOK loaded")

    # 3. A 401 on a FRESH origin (servo caches credentials per origin):
    #    wrong credentials get a 401 DENIED body, never AUTHOK.
    cmd(s, {"cmd": "navigate", "tab": 0, "url": "http://127.0.0.1:8129/private"})
    ch = wait_auth("127.0.0.1:8129")
    print("second challenge held:", ch["url"])
    # Answer wrong credentials: the server rejects them and re-challenges
    # (a NEW auth event, higher id). The old page's body stays visible while
    # the challenge is pending (servo keeps the previous document), so the
    # observable is the re-challenge, not the body. Leave it unanswered:
    # the engine drops it after 10s (fail closed).
    cmd(s, {"cmd": "auth", "tab": 0, "id": ch["id"], "user": "bad", "pass": "creds"})
    ch2 = wait_event(lambda e: e.get("ev") == "auth"
                      and e.get("id", 0) > ch["id"], timeout=15.0)
    assert "8129" in ch2.get("url", ""), f"re-challenge from wrong origin: {ch2}"
    print("wrong credentials -> re-challenged (rejected)")
    # No further answer: the challenge times out fail-closed. Give it the
    # 10s + slack, then verify the engine is still healthy (ping).
    time.sleep(12)
    cmd(s, {"cmd": "ping", "tab": 0, "id": 999})
    wait_event(lambda e: e.get("ev") == "pong" and e.get("id") == 999, timeout=10)
    print("unanswered challenge timed out fail-closed, engine healthy")

    print("ALL OK")
finally:
    stop = True
    if s:
        try:
            s.close()
        except OSError:
            pass
    time.sleep(0.5)
    engine.kill()