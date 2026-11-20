"""Integration tests for scroll/resize/history MCP tools, cookie-aware
session downloads (use_session), and the wait_ready() navigation race fix.
"""
import http.server
import os
import sys
import tempfile
import threading
import time

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tools")
sys.path.insert(0, TOOLS)
import browserr_mcp as m

PORT = 8194

PAGE_A = (
    b"<html><head><title>PageA</title></head>"
    b"<body style='height:3000px;margin:0'>"
    b"<h1 id=h>PAGE-A</h1>"
    b"</body></html>"
)
PAGE_B = (
    b"<html><head><title>PageB</title></head>"
    b"<body><h1 id=h>PAGE-B</h1></body></html>"
)
SECRET_BODY = b"INVOICE-2026-COOKIE-VERIFIED\x00\x01\x02\xff"


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/a":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Set-Cookie", "sid=validtoken; Path=/")
            self.send_header("Content-Length", str(len(PAGE_A)))
            self.end_headers()
            self.wfile.write(PAGE_A)
        elif self.path == "/b":
            # Delay so the old document (Page A) stays readyState=complete
            # while Page B is in flight - exercises the wait_ready race.
            time.sleep(0.8)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(PAGE_B)))
            self.end_headers()
            self.wfile.write(PAGE_B)
        elif self.path == "/secret.bin":
            cookie = self.headers.get("Cookie", "")
            if "sid=validtoken" in cookie:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(SECRET_BODY)))
                self.end_headers()
                self.wfile.write(SECRET_BODY)
            else:
                body = b"unauthorized"
                self.send_response(403)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
threading.Thread(target=server.serve_forever, daemon=True).start()

m.call_tool("browser_start", {})
d = m.STATE.d()
base = f"http://127.0.0.1:{PORT}"

try:
    # 1) Navigate to Page A (sets sid=validtoken cookie)
    r = m.call_tool("browser_navigate", {"url": f"{base}/a", "settle_ms": 8000})
    assert r["settled"], r
    assert d.eval_js("document.title") == "PageA"
    print("page A loaded OK")

    # 2) browser_scroll: wheel down and verify scrollY moves
    before_y = d.eval_js("window.scrollY")
    m.call_tool("browser_scroll", {"dy": 400, "x": 100, "y": 100})
    time.sleep(0.4)
    after_y = d.eval_js("window.scrollY")
    assert after_y > before_y, f"scrollY did not increase: {before_y} -> {after_y}"
    print("browser_scroll OK:", before_y, "->", after_y)

    # 3) browser_resize: change viewport and verify innerWidth/innerHeight
    m.call_tool("browser_resize", {"w": 800, "h": 500})
    deadline = time.time() + 5.0
    dims = None
    while time.time() < deadline:
        dims = d.eval_js("[window.innerWidth, window.innerHeight]")
        if dims == [800, 500]:
            break
        time.sleep(0.1)
    assert dims == [800, 500], f"viewport did not resize: {dims}"
    print("browser_resize OK:", dims)

    # 4) Cookieless download must fail with 403; use_session download succeeds
    #    with exact binary payload intact.
    with tempfile.TemporaryDirectory(prefix="browserr-dl-") as td:
        pub_path = os.path.join(td, "pub.bin")
        try:
            m.call_tool("browser_download",
                        {"url": f"{base}/secret.bin", "path": pub_path})
        except RuntimeError as e:
            assert "403" in str(e), e
            print("cookieless download rejected 403 OK")
        else:
            raise AssertionError("cookieless download should have failed 403")

        ses_path = os.path.join(td, "ses.bin")
        dl = m.call_tool("browser_download",
                         {"url": f"{base}/secret.bin", "path": ses_path,
                          "use_session": True})
        assert dl["bytes"] == len(SECRET_BODY), dl
        with open(ses_path, "rb") as f:
            got = f.read()
        assert got == SECRET_BODY, f"binary mismatch: {got!r}"
        print("session download (use_session=True) OK:", dl["bytes"], "bytes")

    # 5) wait_ready() navigation race: navigate A -> slow B and immediately
    #    call wait_ready() (no wait_load in between). Without the nav marker,
    #    wait_ready returns immediately on Page A while B is still loading.
    d.navigate(f"{base}/b")
    d.wait_ready(timeout=15.0)
    assert d.eval_js("document.title") == "PageB", \
        f"wait_ready returned on stale doc: {d.eval_js('document.title')!r}"
    print("wait_ready navigation race fixed OK")

    # 6) browser_history: back -> PageA, forward -> PageB
    hb = m.call_tool("browser_history", {"action": "back"})
    assert hb["url"].endswith("/a"), hb
    assert d.eval_js("document.title") == "PageA"
    print("browser_history back OK:", hb["url"])

    hf = m.call_tool("browser_history", {"action": "forward"})
    assert hf["url"].endswith("/b"), hf
    assert d.eval_js("document.title") == "PageB"
    print("browser_history forward OK:", hf["url"])

    print("ALL OK")
finally:
    m.call_tool("browser_stop", {})
