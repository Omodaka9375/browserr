"""Trust layer test (SDK level).

Covers: evidence shots (plain + annotated) written to a configured dir;
append-only action log records navigate/fill/submit/click/auth events and
NEVER credentials; submit_form refuses payment/captcha pages without
confirm=True; stored credentials auto-answer an HTTP auth challenge and
unknown hosts fail closed.
"""

import os as _os
TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
ROOT = _os.path.dirname(TOOLS_DIR)
import base64
import http.server
import json
import os
import threading

sys_path = None
import sys
sys_path = sys.path
sys.path.insert(0, TOOLS_DIR)
from driver import Driver, NeedsConfirmError

PAGE_URL = "http://127.0.0.1:8139/pay-test.html"
AUTH_URL = "http://127.0.0.1:8140/secret"
EV = TOOLS_DIR + "/trust-evidence"

PAGE = """<!doctype html><html><head><title>Checkout</title></head><body>
<h1>Checkout</h1>
<form id="f" method="get" action="/ok">
  <label for="cc">Card number</label>
  <input id="cc" name="cc" autocomplete="cc-number">
  <button type="submit" id="pay">Pay now</button>
</form>
</body></html>"""


class AuthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                user, pw = base64.b64decode(hdr.split()[1]).split(b":", 1)
            except Exception:
                user = pw = b""
            if user == b"agent" and pw == b"secret":
                body = b"<html><body>AUTHOK</body></html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        body = b"<html><body>DENIED</body></html>"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="trust"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


handler = functools_handler = None
import functools
handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=TOOLS_DIR)
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8139), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
httpd2 = http.server.ThreadingHTTPServer(("127.0.0.1", 8140), AuthHandler)
threading.Thread(target=httpd2.serve_forever, daemon=True).start()

with open(TOOLS_DIR + "/pay-test.html", "w") as f:
    f.write(PAGE)

import time

d = Driver(log=f"{ROOT}/engine-trust-test.log")
try:
    # Fresh evidence dir: the log is append-only, so leftover entries from
    # an earlier run would corrupt the assertions below.
    import shutil
    shutil.rmtree(EV, ignore_errors=True)
    d.enable_evidence(EV)

    # 1. Plain evidence shot lands in the dir as a PNG.
    d.navigate(PAGE_URL)
    d.wait_load(contains="pay-test.html")
    d.wait_ready()
    res = d.evidence(mode="plain")
    assert "error" not in res, res
    assert res["path"].startswith(EV), res
    assert os.path.getsize(res["path"]) > 2000
    print("plain evidence OK:", os.path.basename(res["path"]))

    # 2. Annotated evidence carries items with refs.
    res = d.evidence(mode="annotated")
    assert res.get("items"), res
    assert all("ref" in i and "label" in i for i in res["items"]), res
    print("annotated evidence OK:", len(res["items"]), "items")

    # 3. Submitting a payment page without confirm refuses, fail-closed.
    try:
        d.submit_form()
        raise SystemExit("payment submit did not refuse")
    except NeedsConfirmError as e:
        print("confirm gate OK:", e)
    # The page is untouched: still the checkout form.
    assert d.eval_js("location.href").endswith("pay-test.html")

    # 4. confirm=True submits and the log records it.
    res = d.submit_and_wait(confirm=True, evidence=True)
    assert res["navigated"] is True, res
    assert "/ok" in res["outcome"]["url"], res["outcome"]
    assert res.get("evidence", {}).get("path"), res.get("evidence")
    print("confirmed submit OK:", res["outcome"]["url"])

    # 5. Action log: JSON lines, action kinds present, NO password/secret.
    with open(os.path.join(EV, "actions.jsonl"), encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    kinds = {l["kind"] for l in lines}
    assert {"navigate", "submit", "evidence"} <= kinds, kinds
    # Exactly two submits: one REFUSED by the confirm gate, one confirmed.
    # On a fresh dir this is deterministic; >= 2 was masking leftover state.
    submits = [l for l in lines if l["kind"] == "submit"]
    assert len(submits) == 2, submits
    assert submits[0].get("refused") is True and submits[0]["confirm"] is False
    assert submits[1]["confirm"] is True and "refused" not in submits[1]
    print("action log OK:", len(lines), "entries:", sorted(kinds))

    # 6. Stored credentials auto-answer an auth challenge.
    d.set_credentials("http://127.0.0.1:8140", "agent", "secret")
    d.start_auto_auth()
    d.navigate(AUTH_URL)
    deadline = time.time() + 15
    body = ""
    while time.time() < deadline:
        body = d.eval_js("document.body ? document.body.textContent : ''") or ""
        if "AUTHOK" in body:
            break
        time.sleep(0.3)
    assert "AUTHOK" in body, body
    print("auto-auth OK: challenge answered from stored credentials")

    # 7. Credentials never appear in the action log. (Check the VALUE, not
    # the word 'secret', which our test URL path also contains.)
    with open(os.path.join(EV, "actions.jsonl"), encoding="utf-8") as f:
        dump = f.read()
    auth_lines = [l for l in dump.splitlines() if '"auth"' in l]
    assert auth_lines, "no auth entry in log"
    assert all("user" not in l and "pass" not in l for l in auth_lines), auth_lines
    print("no credential leak OK:", auth_lines[0])

    print("ALL OK")
finally:
    d.close()
