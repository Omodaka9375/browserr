import sys

import functools
import http.server
import sys
import threading

sys.path.insert(0, r"D:/Code/2026/Browserr/tools")
from driver import Driver

PAGE_URL = "http://127.0.0.1:8133/struct-test.html"

with open(r"D:/Code/2026/Browserr/tools/struct-test.html", "w") as f:
    f.write('<!doctype html><html><head><title>Struct Test</title></head><body>'
            '<h1>Main Heading</h1><h2>Sub</h2>'
            '<form><input name=q placeholder="type here">'
            '<input type=submit value=Search></form>'
            '<a href="/x">A link</a><img src="/i.png" alt="pic"></body></html>')

handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=r"D:/Code/2026/Browserr/tools")
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8133), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

d = Driver(log="D:/Code/2026/Browserr/engine-struct-test.log")
try:
    d.navigate(PAGE_URL)
    d.wait_load(contains="struct-test.html")
    d.wait_ready()
    r = d.read_structure()
    assert r["title"] == "Struct Test", r["title"]
    assert any(h["text"] == "Main Heading" and h["level"] == 1 for h in r["headings"]), r["headings"]
    assert any(b["text"] == "Search" for b in r["buttons"]), r["buttons"]
    q = [i for i in r["inputs"] if i["name"] == "q"]
    assert q, r["inputs"]
    assert q[0]["placeholder"] == "type here", q[0]
    print("headings/buttons/inputs OK")
    print("selectors:", q[0]["selector"], "|",
          [b["selector"] for b in r["buttons"] if b["text"] == "Search"][0])
    # selector must be usable by click_element/set_value
    d.set_value(q[0]["selector"], "structured")
    v = d.eval_js("document.querySelector('input[name=q]').value")
    assert v == "structured", v
    print("set_value via structure selector OK")
finally:
    d.close()
print("ALL OK")