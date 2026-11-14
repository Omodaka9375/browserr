"""browser_navigate fail-last behavior for SPA/redirect quirks."""
import http.server
import os
import sys
import threading
import time

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
import browserr_mcp as m

# Fixture: /slow delays the url commit; /redir rewrites host+query.
PORT = 8171
SLOW_PAGE = (b"<html><head><title>SlowSPA</title></head><body>"
             b"<h1 id=x>SLOW-LOADED</h1></body></html>")


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/redir"):
            self.send_response(302)
            self.send_header("Location",
                             "/dest?utm_source=x&utm_campaign=y&sid=123")
            self.end_headers()
        elif self.path.startswith("/slow"):
            time.sleep(1.2)  # longer than wait_ms in the quirk test
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(SLOW_PAGE)))
            self.end_headers()
            self.wfile.write(SLOW_PAGE)
        else:
            body = (b"<html><head><title>Dest</title></head><body>"
                    b"<h1 id=x>DEST-PAGE</h1></body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
threading.Thread(target=server.serve_forever, daemon=True).start()

m.call_tool("browser_start", {})

# 1) Plain navigation: unchanged behavior.
r = m.call_tool("browser_navigate",
                {"url": f"http://127.0.0.1:{PORT}/dest", "settle_ms": 5000})
assert r["settled"] and "note" not in r, r
print("plain navigation OK:", r["url"])

# 2) Redirect that rewrites the url: must resolve, not contain the target.
r = m.call_tool("browser_navigate",
                {"url": f"http://127.0.0.1:{PORT}/redir?src=share",
                 "settle_ms": 8000})
assert r["settled"], r
assert "utm_source" in r["url"], r
print("redirect rewrite OK:", r["url"])

# 3) The quirk itself: wait_ms too short for the commit. Must NOT raise;
#    it verifies from the event stream and reports settled.
r = m.call_tool("browser_navigate",
                {"url": f"http://127.0.0.1:{PORT}/slow",
                 "wait_ms": 300, "settle_ms": 10000})
assert r["url"].endswith("/slow"), r
assert r["settled"], r
print("slow commit with tiny wait_ms OK:", r["url"], "settled:", r["settled"])

# 4) Unreachable host: servo commits and renders its own error page; the
#    tool must NOT raise - it reports error_page so the agent can react
#    (bad url / proxy / network) without a retry loop.
r = m.call_tool("browser_navigate",
                {"url": "http://127.0.0.1:1/nothing", "wait_ms": 3000,
                 "settle_ms": 5000})
assert r.get("error_page"), r
assert r["settled"] is False, r
print("unreachable host reports error_page OK:", r["url"])

m.call_tool("browser_stop", {})
print("ALL OK")
