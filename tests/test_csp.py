"""Strict-CSP pages: nonce script-src without unsafe-eval.

The 0.9.1 regression: eval routed everything through page-side eval(),
which such sites forbid, and the error leaked as a raw __error dict that
crashed internal callers. Since the engine now wraps every driver script
in its own CSP-exempt injected wrapper (EVAL_WRAPPER_JS), page-side
eval() is never used at all: strict-CSP pages get the full experience,
including promise awaiting, with no fallback and no leaked dicts.
"""
import http.server
import os
import sys
import threading

TOOLS = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(os.path.dirname(TOOLS), "tools")
sys.path.insert(0, TOOLS)
import browserr_mcp as m
from driver import EvalError

PORT = 8193
CSP = ("script-src 'nonce-abc123'; default-src 'none'; img-src 'none'; "
       "style-src 'unsafe-inline'")
PAGE = (b"<html><head><title>CSPPage</title></head><body>"
        b"<h1 id=x>CSP-LOCKED</h1><button id=b>Go</button></body></html>")


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)


server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
threading.Thread(target=server.serve_forever, daemon=True).start()

m.call_tool("browser_start", {})
url = f"http://127.0.0.1:{PORT}/"

# 1) wait_ready must tolerate the CSP-blocked adapter eval and settle.
r = m.call_tool("browser_navigate", {"url": url, "wait_ms": 8000})
assert r["settled"], r
print("navigate + settle on CSP page OK")

# 2) Sync eval works through the direct fallback (never a __error dict).
d = m.STATE.d()
assert d.eval_js("1 + 1") == 2, "sync eval on CSP page"
assert d.eval_js("document.title") == "CSPPage"
assert d.eval_js("document.getElementById('x').textContent") == "CSP-LOCKED"
print("sync eval via CSP fallback OK")

# 3) Internal callers must not see raw dicts: read_page / read_structure /
#    read_outcome all eval on the locked page.
title, text = d.read_page()
assert title == "CSPPage", title
assert "CSP-LOCKED" in text, text
st = d.read_structure()
assert any(e.get("ref") == "e1" or e.get("label") for e in
           (st.get("buttons") or [])), st
out = d.read_outcome()
assert out.get("url", "").endswith("/"), out
print("read_page / read_structure / read_outcome on CSP page OK")

# 4) A failing script raises typed EvalError, not a dict.
try:
    d.eval_js("this.does.not.exist()")
except EvalError:
    print("typed EvalError OK")
else:
    raise AssertionError("expected EvalError on CSP page")

# 5) Promises are awaited on CSP pages too now (the wrapper is
#    CSP-exempt): an async IIFE resolves to its value, not {}.
v = d.eval_js("(async () => 42)()")
assert v == 42, v
print("async IIFE awaited on CSP page OK")

# 5b) The driver leaves no enumerable window globals behind.
leaked = d.eval_js("JSON.stringify(Object.keys(window).filter("
                   "k => k.startsWith('__browserr')))")
assert leaked == "[]", leaked
print("no enumerable __browserr globals OK")

# 6) Non-CSP sanity: the wrapper path still awaits promises.
d.navigate("about:blank")
d.wait_ready()
v = d.eval_js("(async () => 7)()")
assert v == 7, v
print("promise await on normal page OK")

m.call_tool("browser_stop", {})
print("ALL OK")
