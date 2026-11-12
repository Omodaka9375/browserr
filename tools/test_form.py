"""Form model v2 test (SDK level).

Covers the richer read_structure: label resolution (for/wrapping/aria),
constraints, current state, computed validity, select option labels, form
grouping, stable refs, and type-aware writes through set_field/click_ref.
"""

import os as _os
TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
ROOT = _os.path.dirname(TOOLS_DIR)
import functools
import http.server
import sys
import threading

sys.path.insert(0, TOOLS_DIR)
from driver import Driver

PAGE_URL = "http://127.0.0.1:8134/form-test.html"

PAGE = """<!doctype html><html><head><title>Form Test</title></head><body>
<h1>Signup</h1>
<form id="f" name="signup" action="/post" method="post">
  <label for="email">Email address</label>
  <input id="email" name="email" type="email" required
         placeholder="you@example.com" autocomplete="email">
  <label for="zip">ZIP code</label>
  <input id="zip" name="zip" type="text" pattern="[0-9]{5}" maxlength="5">
  <label><input type="checkbox" name="tos" id="tos" required> I accept the terms</label>
  <fieldset><legend>Plan</legend>
    <label><input type="radio" name="plan" value="free"> Free</label>
    <label><input type="radio" name="plan" value="pro"> Pro</label>
  </fieldset>
  <label for="country">Country</label>
  <select id="country" name="country">
    <option value="">choose</option>
    <option value="hu">Hungary</option>
    <option value="us">United States</option>
  </select>
  <label for="dob">Birth date</label>
  <input id="dob" name="dob" type="date">
  <div id="bio" contenteditable="true" role="textbox" aria-label="Short bio"></div>
  <button type="button" id="ping">Ping</button>
  <button type="submit" id="go">Create account</button>
</form>
<div id="out"></div>
<script>
function say(t){ document.getElementById('out').textContent = t; }
document.getElementById('tos').addEventListener('change', function(){ say('tos:' + this.checked); });
document.getElementById('country').addEventListener('change', function(){ say('country:' + this.value); });
document.getElementById('ping').addEventListener('click', function(){ say('pinged'); });
</script>
</body></html>"""

with open(TOOLS_DIR + "/form-test.html", "w") as f:
    f.write(PAGE)

handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=TOOLS_DIR)
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8134), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def field(r, key):
    for i in r["inputs"]:
        if i.get("name") == key or i.get("id") == key:
            return i
    raise SystemExit(f"no input {key}: {[(i.get('name'), i.get('id')) for i in r['inputs']]}")


d = Driver(log=f"{ROOT}/engine-form-test.log")
try:
    d.navigate(PAGE_URL)
    d.wait_load(contains="form-test.html")
    d.wait_ready()
    r = d.read_structure()

    # 1. Labels resolve through <label for>, wrapping <label> and aria-label.
    email = field(r, "email")
    assert email["label"] == "Email address", email["label"]
    tos = field(r, "tos")
    assert tos["label"] == "I accept the terms", tos["label"]
    bio = field(r, "bio")
    assert bio["label"] == "Short bio", bio["label"]
    assert bio["type"] == "richtext", bio["type"]
    print("labels OK:", email["label"], "|", tos["label"], "|", bio["label"])

    # 2. Constraints come from attributes.
    assert email["constraints"].get("required") is True, email.get("constraints")
    assert email["constraints"].get("autocomplete") == "email", email.get("constraints")
    zipf = field(r, "zip")
    assert zipf["constraints"].get("pattern") == "[0-9]{5}", zipf.get("constraints")
    assert zipf["constraints"].get("maxlength") == "5", zipf.get("constraints")
    assert tos["constraints"].get("required") is True, tos.get("constraints")
    print("constraints OK:", email["constraints"], "|", zipf["constraints"])

    # 3. Validity is computed, not guessed: empty required fields are invalid.
    assert email["valid"] is False and "required" in email["invalid"], email
    assert tos["valid"] is False and "required" in tos["invalid"], tos
    assert zipf["valid"] is True, zipf
    print("validity OK: email invalid", email["invalid"], "| tos invalid", tos["invalid"])

    # 4. Selects expose option values AND labels.
    country = [s for s in r["selects"] if s.get("name") == "country"][0]
    assert country["label"] == "Country", country["label"]
    labels = {o["value"]: o["label"] for o in country["options"]}
    assert labels.get("hu") == "Hungary", labels
    print("select options OK:", labels)

    # 5. Forms group their fields and submit controls.
    assert r["forms"], "no forms"
    form = r["forms"][0]
    assert form["id"] == "f" and form["method"] == "post", form
    assert email["ref"] in form["fields"] and tos["ref"] in form["fields"], form
    assert any(b["ref"] in form["submits"] for b in r["buttons"]
               if b["text"] == "Create account"), form
    print("form grouping OK:", form["id"], form["method"], len(form["fields"]), "fields")

    # 6. Refs are unique and every interactive element has one.
    refs = [i["ref"] for i in r["inputs"]] + [b["ref"] for b in r["buttons"]] \
        + [s["ref"] for s in r["selects"]]
    assert len(refs) == len(set(refs)), refs
    assert all(x.startswith("e") and x[1:].isdigit() for x in refs), refs
    print("refs OK:", len(refs), "unique:", refs)

    # 7. Type-aware writes through refs.
    res = d.set_field(zipf["ref"], "12345")
    assert res["ok"] and res["value"] == "12345", res
    r = d.read_structure()
    zipf2 = field(r, "zip")
    assert zipf2["valid"] is True, zipf2
    assert zipf2["value"] == "12345", zipf2
    print("text write by ref OK:", res)

    # 8. Pattern violation is reported as invalid.
    d.set_field(zipf2["ref"], "abc")
    r = d.read_structure()
    zipf3 = field(r, "zip")
    assert zipf3["valid"] is False and "pattern" in zipf3["invalid"], zipf3
    print("pattern invalid OK:", zipf3["invalid"])

    # 9. Checkbox by ref toggles and fires the page handler.
    r = d.read_structure()
    tos2 = field(r, "tos")
    res = d.set_field(tos2["ref"], True)
    assert res["ok"] and res["value"] is True, res
    out = d.eval_js("document.getElementById('out').textContent")
    assert out == "tos:true", out
    print("checkbox by ref OK:", res, "| handler:", out)

    # 10. Select by LABEL (not value) resolves to the right option.
    r = d.read_structure()
    country2 = [s for s in r["selects"] if s.get("name") == "country"][0]
    res = d.set_field(country2["ref"], "Hungary")
    assert res["ok"] and res["value"] == "hu", res
    out = d.eval_js("document.getElementById('out').textContent")
    assert out == "country:hu", out
    print("select by label OK:", res, "| handler:", out)

    # 11. contenteditable takes text.
    r = d.read_structure()
    bio2 = field(r, "bio")
    res = d.set_field(bio2["ref"], "hello bio")
    assert res["ok"] and res["how"] == "richtext", res
    assert d.eval_js("document.getElementById('bio').textContent") == "hello bio"
    print("richtext OK:", res)

    # 12. click_ref clicks a real button and the handler runs.
    r = d.read_structure()
    ping = [b for b in r["buttons"] if b["text"] == "Ping"][0]
    pt = d.click_ref(ping["ref"])
    assert pt and pt["w"] > 0, pt
    # The handler runs after the input event drains; poll briefly.
    import time as _time
    t0 = _time.time()
    out = ""
    while _time.time() - t0 < 5.0:
        out = d.eval_js("document.getElementById('out').textContent")
        if out == "pinged":
            break
        _time.sleep(0.1)
    assert out == "pinged", f"click_ref handler did not run: {out!r}"
    print("click_ref OK:", ping["ref"], pt)

    # 13. A stale ref (page reloaded since the read) resolves to None.
    d.navigate(PAGE_URL + "?fresh=1")
    d.wait_load(contains="fresh=1")
    d.wait_ready()
    assert d.click_ref(ping["ref"]) is None, "stale ref should not resolve"
    print("stale ref guard OK")

    # 14. Writing by CSS selector still works alongside refs.
    res = d.set_field("#email", "a@b.co")
    assert res["ok"] and res["value"] == "a@b.co", res
    print("selector write OK:", res)

    print("ALL OK")
finally:
    d.close()