"""Proxy + document-status + settle test.

1. Auth proxy: spawn the engine with --proxy=http://proxyuser:proxypass@
   127.0.0.1:8888 (the fixture proxy REJECTS bad/missing auth with 407);
   https://example.com must load through the tunnel.
2. Bad auth: --proxy with wrong credentials must FAIL to load (page never
   commits) - proves the credentials actually ride the CONNECT.
3. document_status: a served 404 must report status 404 through the ring
   (the block-vs-dead signal).

Requires tools/logging_proxy.py running on 127.0.0.1:8888 and network
access to example.com.
"""

import os as _os
TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
ROOT = _os.path.dirname(TOOLS_DIR)
import sys
import time

sys.path.insert(0, TOOLS_DIR)
from driver import Driver

PORT = 8888
USER = "proxyuser"
PASS = "proxypass"

# --- 1. authenticated proxy tunnel ---
d = Driver(engine_args=[f"--proxy=http://{USER}:{PASS}@127.0.0.1:{PORT}"],
           log=f"{ROOT}/engine-proxy-test.log")
try:
    d.navigate("https://example.com/")
    d.wait_load(contains="example.com")
    d.wait_ready()
    title = d.eval_js("document.title")
    assert "Example" in (title or ""), title
    print("auth-proxy OK:", title)
finally:
    d.close()

# --- 2. wrong credentials must not render anything ---
# NB: servo fires the url event on load START (even for a rejected tunnel),
# so the url arriving is NOT proof of success. The page content is: with
# wrong credentials the proxy 407s the CONNECT, the document errors out and
# the body never renders.
d = Driver(engine_args=[f"--proxy=http://{USER}:WRONG@127.0.0.1:{PORT}"],
           log=f"{ROOT}/engine-proxy-test.log")
try:
    d.navigate("https://example.com/")
    try:
        d.wait_load(contains="example.com", timeout=20)
    except TimeoutError:
        pass
    deadline = time.time() + 10
    rendered = False
    while time.time() < deadline:
        try:
            if "Example" in (d.eval_js("document.title") or ""):
                rendered = True
                break
        except Exception:
            pass
        time.sleep(0.5)
    if rendered:
        raise SystemExit("FAIL: proxy accepted wrong credentials (page rendered)")
    print("bad-auth OK: tunnel rejected, page never rendered")
finally:
    d.close()

# --- 3. document status probe: 404 vs 200 ---
import functools
import http.server
import threading

PAGE_404 = ("<!doctype html><html><body><h1>404 Not Found</h1>"
            "<p>the fake-404 a blocking site would serve</p></body></html>")
PAGE_200 = "<!doctype html><html><body><h1>real listing</h1></body></html>"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/missing"):
            body = PAGE_404.encode()
            self.send_response(404)
        else:
            body = PAGE_200.encode()
            self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        # The document probe sends HEAD; answer with the same status.
        if self.path.startswith("/missing"):
            self.send_response(404)
        else:
            self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8144), Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

d = Driver(log=f"{ROOT}/engine-proxy-test.log")
try:
    d.navigate("http://127.0.0.1:8144/present")
    d.wait_load(contains="/present")
    d.wait_ready()
    status, ctype = d.document_status()
    assert status == 200, status
    assert ctype and "text/html" in ctype, ctype
    print("document-status 200 OK:", status, ctype)

    d.navigate("http://127.0.0.1:8144/missing")
    d.wait_load(contains="/missing")
    d.wait_ready()
    status, _ = d.document_status()
    assert status == 404, status
    print("document-status 404 OK:", status)

    # The probe also lands in the ring.
    ring = d.net_evidence()
    probes = [e for e in ring if e.get("kind") == "document-probe"]
    assert probes and probes[-1].get("status") == 404, probes
    print("ring document-probe OK")
finally:
    d.close()

try:
    if _os.path.exists(f"{ROOT}/engine-proxy-test.log"):
        _os.remove(f"{ROOT}/engine-proxy-test.log")
except OSError:
    pass  # engine log handle lingers briefly; the file is gitignored
print("ALL OK")
