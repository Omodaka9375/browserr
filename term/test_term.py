"""Offline test for the TUI: Term handlers + dirty blit + synthetic input."""
import sys, os, time, json, io
HERE = os.path.dirname(os.path.abspath(__file__))
for p in (HERE, os.path.join(HERE, "..", "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)
from driver import Driver, save_png
from browserr_term import Term, HalfblockDirty, to_rgb, parse_keys

d = Driver(ephemeral=True)
t = Term(d, 100, 30)
t.go("file:///" + os.path.join(HERE, "term-fixture.html").replace("\\", "/"))
d.wait_load(); d.wait_ready(); time.sleep(0.5)

# 1) dirty render path with real frames
w, h, bgra = d.screenshot()
img = to_rgb(bgra, w, h)
r = HalfblockDirty(100, 29, io.StringIO())
n_full = r.render(img)
n_same = r.render(img)
assert n_full > 2000 and n_same == 0, (n_full, n_same)
print("dirty render ok: full", n_full, "cells; repeat", n_same)

# 2) Native PNG payload is a valid full frame.
png = d.native_frame(format="png", force=True)[0][4]
assert png[:8] == b"\x89PNG\r\n\x1a\n" and png.endswith(b"IEND\xaeB`\x82")
print("native PNG ok:", len(png), "bytes")

# 3) synthetic events through the SAME parse_keys the TUI uses
buf = b"\x1b[<0;10;5M"      # mouse press
buf += b"\x1b[<64;10;5M"    # wheel up
buf += b"\x1b[<65;10;5M"    # wheel down
buf += b"x\r"
evs, rest = parse_keys(buf)
kinds = [(e["kind"], e.get("action") or e.get("name") or e.get("text", "")) for e in evs]
print("events parsed:", kinds)
assert ("mouse", "press") in kinds
assert ("mouse", "wheel") in kinds
assert ("text", "x") in kinds
assert rest == b""

# 4) handlers consume everything without error
for e in evs:
    if e["kind"] == "mouse":
        t.handle_mouse(e)
    else:
        t.handle_key(e)
print("handlers ok")

# 5) click through driver, page reacts
rect = d.eval_js("(function(){var r=document.querySelector('button').getBoundingClientRect();return JSON.stringify({x:r.x,y:r.y,w:r.width,h:r.height})})()")
rr = json.loads(rect) if isinstance(rect, str) else rect
d.click(int(rr["x"] + rr["w"] / 2), int(rr["y"] + rr["h"] / 2))
time.sleep(0.5)
txt = d.eval_js("document.querySelector('button').textContent")
assert txt != "clickme", "button did not react"
print("click ok:", txt)

# 6) URL bar flow
t.handle_key({"kind": "ctrl", "c": "L"})
assert t.url_mode
for ch in "file:///" + os.path.join(HERE, "term-fixture.html").replace("\\", "/"):
    t.handle_key({"kind": "text", "text": ch})
t.handle_key({"kind": "key", "name": "Enter", "text": ""})
d.wait_load()
assert t.url == "file:///" + os.path.join(HERE, "term-fixture.html").replace("\\", "/"), t.url
print("urlbar ok")

print("ALL OK")
d.close()
