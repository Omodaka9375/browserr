"""`$websocket` filter rules block a WebSocket handshake.

A WebSocket handshake reaches the engine's request hook as an http(s)
fetch with no destination, exactly like fetch() and XHR, so the filter
list classified it as "other" and no `$websocket` rule ever matched it.
The hook now reads the handshake's `Upgrade: websocket` header.

1. With `*$websocket` in the list, a page's `new WebSocket(...)` never
   reaches the server, and a fetch() from the same page still does.
2. With a list that names another host, the handshake reaches the server
   (the control: the server can see one when nothing blocks it).
"""
import http.server
import os
import sys
import tempfile
import threading
import time

TOOLS = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(os.path.dirname(TOOLS), "tools")
ROOT = os.path.dirname(TOOLS)
sys.path.insert(0, TOOLS)
from driver import Driver

PORT = 8198
PAGE = b"""<!doctype html><html><head><title>sockets</title></head><body>
<h1>sockets</h1>
<script>
window.__fetched = "pending";
fetch("/data").then(r => r.text()).then(t => { window.__fetched = t; })
              .catch(() => { window.__fetched = "failed"; });
window.__socket = "pending";
try {
  const ws = new WebSocket("ws://127.0.0.1:%d/socket");
  ws.onerror = () => { window.__socket = "error"; };
  ws.onclose = () => { window.__socket = "closed"; };
} catch (e) { window.__socket = "threw"; }
</script></body></html>""" % PORT

seen = []


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        upgrade = (self.headers.get("Upgrade") or "").lower()
        seen.append((self.path, upgrade))
        if self.path == "/socket":
            # Refuse the handshake; the test only cares whether it arrived.
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"data-ok" if self.path == "/data" else PAGE
        ctype = "text/plain" if self.path == "/data" else "text/html"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
threading.Thread(target=server.serve_forever, daemon=True).start()

tmp = tempfile.mkdtemp(prefix="browserr-adblock-ws-")


def run(rules):
    """Load the page under a filter list; return (socket paths seen, fetched)."""
    del seen[:]
    path = os.path.join(tmp, "list-%d.txt" % len(os.listdir(tmp)))
    with open(path, "w", encoding="utf-8") as f:
        f.write(rules + "\n")
    d = Driver(engine_args=["--adblock-list=" + path],
               log=f"{ROOT}/engine-adblock-websocket-test.log")
    try:
        d.navigate(f"http://127.0.0.1:{PORT}/")
        d.wait_load(contains=str(PORT))
        d.wait_ready()
        deadline = time.time() + 10
        fetched = None
        while time.time() < deadline:
            fetched = d.eval_js("window.__fetched")
            if fetched not in (None, "pending"):
                break
            time.sleep(0.2)
        # The handshake is sent as the page's script runs; give a blocked
        # one time to have shown up if it was going to.
        time.sleep(3)
        return [p for p, _ in seen if p == "/socket"], fetched
    finally:
        d.close()


sockets, fetched = run("*$websocket")
assert fetched == "data-ok", f"fetch() was blocked by a websocket rule: {fetched!r}"
assert sockets == [], f"websocket handshake reached the server: {sockets}"
print("$websocket rule blocks the handshake, fetch() still loads OK")

sockets, fetched = run("||blocked.invalid^")
assert fetched == "data-ok", fetched
assert sockets == ["/socket"], f"control saw no handshake: {seen}"
upgrade = [u for p, u in seen if p == "/socket"][0]
assert upgrade == "websocket", f"handshake carried Upgrade: {upgrade!r}"
print("unblocked handshake reaches the server OK")

print("ALL OK")
