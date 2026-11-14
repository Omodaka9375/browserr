"""Form flow test (SDK level).

Covers the batch write and submit cycle: a JS-validated form that refuses
an empty submit (outcome readback sees the error), fill_form writing a
whole form in one round trip, then a real POST submit that navigates and
reports the landing page.
"""

import os as _os
TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
ROOT = _os.path.dirname(TOOLS_DIR)
import http.server
import sys
import threading

sys.path.insert(0, TOOLS_DIR)
from driver import Driver

PAGE_URL = "http://127.0.0.1:8135/form-flow.html"

PAGE = """<!doctype html><html><head><title>Form Flow</title></head><body>
<h1>Contact</h1>
<form id="f" method="post" action="/done">
  <label for="email">Email</label>
  <input id="email" name="email" type="email" required>
  <label for="zip">ZIP</label>
  <input id="zip" name="zip" required pattern="[0-9]{5}">
  <label><input type="checkbox" name="tos" id="tos" required> Accept the terms</label>
  <div id="err" role="alert"></div>
  <button type="submit" id="go">Send</button>
</form>
<form id="xorigin" method="get" action="http://localhost:8135/done">
  <input id="xo" name="q" value="cross">
  <button type="submit" id="xgo">Cross</button>
</form>
<script>
document.getElementById('f').addEventListener('submit', function (e) {
  var bad = [];
  if (!document.getElementById('email').value) bad.push('email');
  if (!document.getElementById('zip').value) bad.push('zip');
  if (!document.getElementById('tos').checked) bad.push('tos');
  if (bad.length) {
    e.preventDefault();
    document.getElementById('err').textContent = 'Missing: ' + bad.join(', ');
  }
});
</script>
</body></html>"""

with open(TOOLS_DIR + "/form-flow.html", "w") as f:
    f.write(PAGE)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=TOOLS_DIR, **kwargs)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        body = (b"<!doctype html><html><head><title>Done</title></head>"
                b"<body><h1>THANK YOU</h1></body></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8135), Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def field(r, key):
    for i in r["inputs"]:
        if i.get("name") == key or i.get("id") == key:
            return i
    raise SystemExit(f"no input {key}")


def button(r, text):
    for b in r["buttons"]:
        if b.get("text") == text:
            return b
    raise SystemExit(f"no button {text}: {[b.get('text') for b in r['buttons']]}")


def structure_with_fields(timeout=10.0):
    """read_structure once the page actually has fields (a fresh document
    can report readyState complete before its DOM is there)."""
    import time as _time
    t0 = _time.time()
    while _time.time() - t0 < timeout:
        r = d.read_structure()
        if r["inputs"]:
            return r
        _time.sleep(0.2)
    raise SystemExit("no inputs appeared")


d = Driver(log=f"{ROOT}/engine-flow-test.log")
try:
    d.navigate(PAGE_URL)
    d.wait_load(contains="form-flow.html")
    d.wait_ready()

    # 1. An empty submit is prevented by the page: the outcome reports why.
    r = d.read_structure()
    res = d.submit_form(button(r, "Send")["ref"])
    assert res["ok"], res
    out = d.read_outcome()
    assert any("Missing:" in a for a in out["alerts"]), (
        res, out, d.eval_js("document.getElementById('err').textContent"),
        d.eval_js("window.__browserr_submit_seen"))
    assert "form-flow.html" in out["url"], out["url"]
    names = {f["name"] for f in out["invalid_fields"]}
    assert {"email", "zip"} <= names, out["invalid_fields"]
    print("prevented submit OK:", out["alerts"][0], "| invalid:", sorted(names))

    # 2. One round trip fills the whole form.
    r = d.read_structure()
    plan = {field(r, "email")["ref"]: "a@b.co",
            field(r, "zip")["ref"]: "12345",
            field(r, "tos")["ref"]: True}
    res = d.fill_form(plan)
    assert res["ok"], res
    assert all(v["ok"] for v in res["fields"].values()), res
    print("fill_form OK:", {k: v["how"] for k, v in res["fields"].items()})

    # 3. The read-back confirms the form is now valid and the alert cleared.
    r = d.read_structure()
    for key in ("email", "zip", "tos"):
        f = field(r, key)
        assert f["valid"] is True, f
        assert not f.get("invalid"), f
    assert field(r, "zip")["value"] == "12345", field(r, "zip")
    assert field(r, "tos")["checked"] is True, field(r, "tos")
    print("read-back OK: email/zip/tos all valid")

    # 4. A real POST submit navigates and the outcome reports the new page.
    res = d.submit_and_wait(button(r, "Send")["ref"])
    assert res["navigated"] is True, res
    assert "/done" in res["outcome"]["url"], res["outcome"]
    assert res["outcome"]["title"] == "Done", res["outcome"]
    body = (d.eval_js("document.body.textContent") or "").strip()
    assert body == "THANK YOU", body
    print("submit navigated OK:", res["outcome"]["url"], "|", body)

    # 5. Network idle on a quiet page returns promptly.
    assert d.wait_network_idle(quiet_ms=300, timeout=5.0) is True
    print("network idle OK")

    # 6. Submitting with no ref falls back to the first form on the page.
    d.navigate(PAGE_URL)
    d.wait_load(contains="form-flow.html")
    d.wait_ready()
    r = structure_with_fields()
    d.fill_form({field(r, "email")["ref"]: "x@y.z",
                 field(r, "zip")["ref"]: "99999",
                 field(r, "tos")["ref"]: True})
    res = d.submit_and_wait(None)
    assert res["navigated"] is True, res
    assert res["outcome"]["title"] == "Done", res["outcome"]
    print("submit without ref OK")

    # 7. Cross-origin GET submit to a MORE-LOCAL target: servo 0.8 on
    # WINDOWS blocks page-initiated navigations that go public->local
    # (and IP->localhost), so the form silently does nothing; on Linux
    # the same submit navigates. Either way the driver must report
    # honestly: navigated true + landed, or navigated false + nav_note,
    # and the embedder-level navigate() always lands.
    import time as _t
    d.navigate(PAGE_URL)
    d.wait_load(contains="form-flow.html")
    d.wait_ready()
    r = structure_with_fields()
    res = d.submit_and_wait('#xorigin')
    assert res["submit"].get("ok"), res
    if res["navigated"]:
        assert "localhost" in res["outcome"]["url"], res["outcome"]
        print("cross-origin submit navigated OK")
    else:
        assert "nav_note" in res, res
        print("local-network block detected (Windows servo)")
    d.navigate("http://localhost:8135/done")
    d.wait_load(contains="/done")
    d.wait_ready()
    deadline = _t.time() + 10
    while _t.time() < deadline:
        if "localhost" in (d.eval_js("location.href") or ""):
            break
        _t.sleep(0.3)
    assert "localhost" in (d.eval_js("location.href") or "")
    print("local-network block detected + navigate() bypass OK")

    print("ALL OK")
finally:
    d.close()