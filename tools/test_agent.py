"""Agent SDK test: ping, ephemeral profiles, read_page/links/forms/click_text/fill.

Covers the reliability + extraction features an automation agent relies on.
"""
import functools
import http.server
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, r"D:/Code/2026/Browserr/tools")
from driver import Driver

PAGE = """<!doctype html><html><head><title>Browserr Agent Test</title></head><body style='margin:0'>
<h1>Browserr Agent Test</h1>
<p>Welcome to the <b>sample page</b> for agent extraction.</p>
<a id=about href='/about.html'>About page</a>
<a href='https://example.com' style='display:none'>hidden link</a>
<form action='/search' method='get'>
  <input name='q' placeholder='Search here'>
  <input type='submit' value='Go'>
</form>
</body></html>"""

ABOUT = """<!doctype html><html><head><title>About</title></head><body><h1>About</h1>
<p>This is the about page.</p></body></html>"""

handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=r"D:/Code/2026/Browserr/tools")
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8129), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

with open(r"D:/Code/2026/Browserr/tools/agent-test.html", "w") as f:
    f.write(PAGE)
with open(r"D:/Code/2026/Browserr/tools/about.html", "w") as f:
    f.write(ABOUT)

# 1. ping works headless
d = Driver(log="D:/Code/2026/Browserr/engine-agent-test.log")
try:
    info = d.ping()
    assert info["tabs"] >= 1, info
    print("ping OK:", info)

    d.navigate("http://127.0.0.1:8129/agent-test.html")
    d.wait_load(contains="agent-test.html")
    d.wait_ready()

    # 2. extraction: title + markdown-ish text with the visible link only
    title, text = d.read_page()
    assert title == "Browserr Agent Test", title
    assert "Welcome to the sample page" in text, text
    assert "[About page](http://127.0.0.1:8129/about.html)" in text, text
    assert "example.com" not in text, text  # hidden link must not appear
    print("read_page OK")

    # 3. links: visible ones only, with geometry
    ls = d.links()
    hrefs = [l["href"] for l in ls]
    assert "http://127.0.0.1:8129/about.html" in hrefs, ls
    assert all("https://example.com" not in h for h in hrefs), hrefs
    about = next(l for l in ls if "about.html" in l["href"])
    assert about["w"] > 0 and about["h"] > 0, about
    print("links OK")

    # 4. forms: fields with geometry
    fs = d.forms()
    assert len(fs) == 1, fs
    q = next(f for f in fs[0]["fields"] if f["name"] == "q")
    assert q["placeholder"] == "Search here", q
    assert q["w"] > 0, q
    print("forms OK")

    # 5. fill by name
    assert d.fill("q", "agent query") is True
    assert d.eval_js("document.getElementsByName('q')[0].value") == "agent query"
    print("fill OK")

    # 6. click_text navigates via the real link
    assert d.click_text("About page") is not None
    d.wait_for(lambda e: e.get("ev") == "url" and "about.html" in e.get("url", ""), timeout=10)
    d.wait_ready()
    title, text = d.read_page()
    assert title == "About" and "about page" in text, (title, text)
    print("click_text OK")
finally:
    d.close()

# 7. ephemeral: close() shuts the engine down gracefully and the temp profile
#    directory the engine created is gone afterwards.
before = set(os.listdir(tempfile.gettempdir()))
d2 = Driver(ephemeral=True, log="D:/Code/2026/Browserr/engine-agent-eph.log")
try:
    d2.navigate("http://127.0.0.1:8129/about.html")
    d2.wait_load(contains="about.html")
    info = d2.ping()
    print("ephemeral ping OK:", info)
finally:
    t0 = time.time()
    d2.close()
    waited = time.time() - t0
    assert waited < 4, f"engine took {waited:.1f}s to exit after socket close"
    print(f"ephemeral close OK ({waited:.2f}s, graceful socket shutdown)")

# The engine's detached helper waits for the pid to exit, then deletes; give
# it a few seconds before checking for leftovers.
deadline = time.time() + 8
while time.time() < deadline:
    after = set(os.listdir(tempfile.gettempdir()))
    gone = [n for n in after - before if n.startswith("browserr-ephemeral-")]
    if not gone:
        break
    time.sleep(0.5)
after = set(os.listdir(tempfile.gettempdir()))
gone = [n for n in after - before if n.startswith("browserr-ephemeral-")]
assert not gone, f"ephemeral dirs left behind: {gone}"
print("ephemeral cleanup OK")

print("ALL OK")
