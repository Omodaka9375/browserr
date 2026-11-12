"""Handoff primitives test (SDK level).

pause() blocks every mutating action with PausedError while reads keep
working; resume() restores control; detect_handoff() spots CAPTCHA, OTP,
password and payment fields.
"""

import os as _os
TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
ROOT = _os.path.dirname(TOOLS_DIR)
import functools
import http.server
import sys
import threading

sys.path.insert(0, TOOLS_DIR)
from driver import Driver, PausedError

PAGE_URL = "http://127.0.0.1:8138/handoff-test.html"

PAGE = """<!doctype html><html><head><title>Handoff</title></head><body>
<h1>Login</h1>
<form>
  <label for="user">User</label>
  <input id="user" name="user" autocomplete="username">
  <label for="pass">Password</label>
  <input id="pass" type="password" name="pass" autocomplete="current-password">
  <label for="otp">One-time code</label>
  <input id="otp" name="otp" autocomplete="one-time-code">
  <iframe src="https://www.google.com/recaptcha/api2/anchor"></iframe>
  <button type="submit">Sign in</button>
</form>
</body></html>"""

with open(TOOLS_DIR + "/handoff-test.html", "w") as f:
    f.write(PAGE)

handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=TOOLS_DIR)
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8138), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def must_raise(fn, what):
    try:
        fn()
    except PausedError as e:
        return str(e)
    raise SystemExit(f"{what} did not raise PausedError")


d = Driver(log=f"{ROOT}/engine-handoff-test.log")
try:
    d.navigate(PAGE_URL)
    d.wait_load(contains="handoff-test.html")
    d.wait_ready()

    # 1. detect_handoff: captcha iframe + otp + password all reported.
    h = d.detect_handoff()
    assert h["handoff"] is True, h
    kinds = {r["kind"] for r in h["reasons"]}
    assert {"captcha", "otp", "password"} <= kinds, h["reasons"]
    assert "captcha" in h["paused_hint"], h["paused_hint"]
    print("detect OK:", sorted(kinds), "|", h["paused_hint"])

    # 2. pause blocks mutation across every write path.
    d.pause("captcha wall")
    assert d.paused == "captcha wall", d.paused

    msg = must_raise(lambda: d.navigate("http://example.com"), "navigate")
    print("navigate blocked:", msg)
    msg = must_raise(lambda: d.click(10, 10), "click")
    msg = must_raise(lambda: d.key("text", "x"), "key")
    msg = must_raise(lambda: d.wheel(5), "wheel")
    msg = must_raise(lambda: d.set_field("#user", "bob"), "set_field")
    msg = must_raise(lambda: d.fill("user", "bob"), "fill")
    msg = must_raise(lambda: d.fill_form({"#user": "bob"}), "fill_form")
    msg = must_raise(lambda: d.submit_form(), "submit_form")
    msg = must_raise(lambda: d.set_value("#user", "bob"), "set_value")
    r_now = d.read_structure()
    real_refs = ([i["ref"] for i in r_now["inputs"]]
                 + [s["ref"] for s in r_now["selects"]]
                 + [b["ref"] for b in r_now["buttons"]])
    assert real_refs, "page should have interactive elements"
    msg = must_raise(lambda: d.click_ref(real_refs[0]), "click_ref")
    print("all writers blocked")

    # 3. reads still work while paused: the agent can watch the page.
    r = d.read_structure()
    assert r["title"] == "Handoff", r["title"]
    assert d.detect_handoff()["handoff"] is True
    w, hgt, bgra = d.screenshot()
    assert w > 0 and len(bgra) == w * hgt * 4
    print("reads OK while paused: structure, detect, screenshot")

    # 4. resume restores control.
    d.resume()
    assert d.paused is None
    res = d.set_field("#user", "alice")
    assert res["ok"] and res["value"] == "alice", res
    print("resume OK:", res)

    # 5. a clean page reports no handoff.
    with open(TOOLS_DIR + "/handoff-clean.html", "w") as f:
        f.write('<!doctype html><html><head><title>Clean</title></head>'
                '<body><input id="q" name="q" autocomplete="off">'
                '<input type="password" id="p" name="p" value="stored" '
                'autocomplete="new-password"></body></html>')
    d.navigate("http://127.0.0.1:8138/handoff-clean.html")
    d.wait_load(contains="handoff-clean.html")
    d.wait_ready()
    h = d.detect_handoff()
    assert h["handoff"] is False and h["reasons"] == [], h
    print("clean page OK: no handoff (prefilled password not flagged)")

    print("ALL OK")
finally:
    d.close()
