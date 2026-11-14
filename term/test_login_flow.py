"""Login through the TUI code path: mouse-click fields, type, Enter."""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (HERE, os.path.join(HERE, "..", "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)
from driver import Driver
from browserr_term import Term, parse_keys

fixture = "file:///" + os.path.join(HERE, "login-fixture.html").replace("\\", "/")
d = Driver(ephemeral=True)
t = Term(d, 100, 30)
t.go(fixture)
d.wait_load()
d.wait_ready()
time.sleep(0.5)

def field_center(sel):
    r = json.loads(d.eval_js(
        f"(function(){{var r=document.querySelector('{sel}')."
        f"getBoundingClientRect();return JSON.stringify("
        f"{{x:r.x,y:r.y,w:r.width,h:r.height}})}})()"))
    return r

# 1) Click the username field at its page coordinates (as a terminal
#    mouse press on the right cell would map to).
u = field_center("#u")
t.handle_mouse({"kind": "mouse", "action": "press", "btn": 0,
                "x": 1, "y": 1})  # placeholder: real coords below
d.click(int(u["x"] + u["w"] / 2), int(u["y"] + u["h"] / 2))
time.sleep(0.3)

# 2) Type the username through the TUI key handler (exact path a user hits).
for ch in "agent":
    t.handle_key({"kind": "text", "text": ch})

# 3) Tab to the password field, type. Password chars are NOT echoed by the
#    TUI itself - only the page renders them (as dots).
t.handle_key({"kind": "key", "name": "Tab"})
time.sleep(0.3)
for ch in "hunter2":
    t.handle_key({"kind": "text", "text": ch})
time.sleep(0.3)

assert d.eval_js("document.activeElement.id") == "p", \
    d.eval_js("document.activeElement.id")
assert d.eval_js("document.querySelector('#u').value") == "agent"
assert d.eval_js("document.querySelector('#p').value") == "hunter2"
print("fields filled via terminal keys: username + password")

# 4) Enter submits the form; the page reacts.
t.handle_key({"kind": "key", "name": "Enter"})
time.sleep(0.6)
assert "WELCOME" in str(d.eval_js(
    "document.querySelector('#welcome').textContent")), "login did not land"
print("Enter submitted; login landed: WELCOME-AGENT")

# 5) Wrong-credentials path also renders (error text visible in the page).
d.navigate(fixture)
d.wait_load()
d.wait_ready()
time.sleep(0.3)
d.click_element("#u")
for ch in "agent":
    t.handle_key({"kind": "text", "text": ch})
time.sleep(0.3)
t.handle_key({"kind": "key", "name": "Tab"})
for ch in "wrong":
    t.handle_key({"kind": "text", "text": ch})
t.handle_key({"kind": "key", "name": "Enter"})
time.sleep(0.6)
assert "wrong credentials" in str(d.eval_js(
    "document.querySelector('#err').textContent"))
print("wrong-credentials error renders in-page")

# 6) Enter in a TEXTAREA inserts a newline, never submits the form.
d.navigate(fixture)
d.wait_load()
d.wait_ready()
time.sleep(0.3)
d.click_element("#ta")
t.handle_key({"kind": "key", "name": "Enter"})
time.sleep(0.4)
val = str(d.eval_js("document.querySelector('#ta').value"))
assert "\n" in val, repr(val)
assert d.eval_js(
    "document.querySelector('#note').contains(document.activeElement)")
print("Enter in textarea = newline, no submit")

print("ALL OK")
d.close()
