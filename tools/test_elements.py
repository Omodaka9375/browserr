"""Element commands test: click_element, set_value, wait_for_selector.

Drives a local page with a search form, a delayed SPA-rendered element and a
click counter, all via CSS selectors - no coordinates, no text guessing.
"""
import functools
import http.server
import sys
import threading
import time

sys.path.insert(0, r"D:/Code/2026/Browserr/tools")
from driver import Driver

PAGE = """<!doctype html><html><head><title>Element Test</title></head>
<style>body{font:14px sans-serif;margin:24px}input{margin:4px 8px;padding:6px}
#late{display:none;margin-top:400px}</style>
<body>
<h1>Element commands</h1>
<input id="q" name="q" placeholder="search"><button id="go">Search</button>
<span id="echo"></span>
<div id="late"><button id="late-btn">Late</button><span id="late-out"></span></div>
<script>
var clicks = 0;
document.getElementById('go').addEventListener('click', function() {
  var q = document.getElementById('q');
  document.getElementById('echo').textContent = 'typed:' + q.value;
});
document.getElementById('late-btn').addEventListener('click', function() {
  document.getElementById('late-out').textContent = 'late clicked';
});
setTimeout(function() {
  document.getElementById('late').style.display = 'block';
}, 1500);
</script></body></html>"""

handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=r"D:/Code/2026/Browserr/tools")
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8132), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
with open(r"D:/Code/2026/Browserr/tools/elements-test.html", "w") as f:
    f.write(PAGE)

d = Driver(log="D:/Code/2026/Browserr/engine-elements-test.log")
try:
    d.navigate("http://127.0.0.1:8132/elements-test.html")
    d.wait_load(contains="elements-test.html")
    d.wait_ready()

    # set_value: native setter + input/change events
    assert d.set_value("#q", "hello selector") is True
    val = d.eval_js("document.getElementById('q').value")
    assert val == "hello selector", val
    print("set_value OK:", val)

    # click_element: real pointer at the button's center
    d.click_element("#go")
    d.wait_for(lambda e: False, timeout=0.01) if False else None
    time.sleep(0.4)
    echo = d.eval_js("document.getElementById('echo').textContent")
    assert echo == "typed:hello selector", echo
    print("click_element OK:", echo)

    # wait_for_selector: element appears 1.5s in (after page load),
    # off-screen - element_rect must scroll it into view first.
    rect = d.wait_for_selector("#late-btn", timeout=10)
    assert rect and rect["w"] > 0 and rect["h"] > 0, rect
    print("wait_for_selector OK:", rect)

    # click_element on the late button (scrolled + clicked)
    d.click_element("#late-btn", timeout=5)
    time.sleep(0.4)
    out = d.eval_js("document.getElementById('late-out').textContent")
    assert out == "late clicked", out
    print("scrolled click OK:", out)

    # timeout path: a selector that never matches raises
    try:
        d.wait_for_selector("#nope-never", timeout=1.5)
        raise SystemExit("FAIL: expected TimeoutError")
    except TimeoutError as e:
        print("timeout path OK:", str(e)[:50])
finally:
    d.close()

import os
os.remove(r"D:/Code/2026/Browserr/tools/elements-test.html")
print("ALL OK")