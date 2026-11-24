"""Event strings carry whatever the page put in them.

The engine built event JSON by hand and escaped only quotes, backslashes
and newlines, so a console message with a tab (or a title with any other
control character) produced invalid JSON. The driver's reader thread died
on it without marking the engine dead, and every later wait hung until
its timeout. Events must parse, keep their characters, and never end the
reader.
"""
import http.server
import os
import sys
import threading
import time

TOOLS = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(os.path.dirname(TOOLS), "tools")
sys.path.insert(0, TOOLS)
import browserr_mcp as m

PORT = 8197
PAGE = (b"<html><head><title>plain</title></head><body><h1>events</h1>"
        b"</body></html>")


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)


server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
threading.Thread(target=server.serve_forever, daemon=True).start()

m.call_tool("browser_start", {})
r = m.call_tool("browser_navigate",
                {"url": f"http://127.0.0.1:{PORT}/", "wait_ms": 8000})
assert r["settled"], r
d = m.STATE.d()

# 1) A console message with a tab, a vertical tab and a control byte.
message = "col1\tcol2\x0bcol3\x01end \"quoted\" back\\slash"
d.eval_js("console.log(%s)" % __import__("json").dumps(message))
seen = d.wait_console(contains="col1", timeout=10.0)
assert seen["message"] == message, repr(seen["message"])
print("console message with control characters OK")

# 2) A title with a control character the HTML title getter keeps.
title = "left\x0bright"
d.eval_js("document.title = %s" % __import__("json").dumps(title))
event = d.wait_for(lambda e: e.get("ev") == "title"
                   and "left" in e.get("title", ""), timeout=10.0)
assert event["title"] == title, repr(event["title"])
print("title with a control character OK")

# 3) The reader is still alive: a fresh eval and a ping both answer.
time.sleep(0.5)
assert d.eval_js("1 + 1") == 2
assert d.ping(timeout=5.0)["tabs"] >= 1
print("reader alive after control-character events OK")

m.call_tool("browser_stop", {})
print("ALL OK")
