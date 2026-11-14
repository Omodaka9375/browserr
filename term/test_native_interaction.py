"""Native viewport, scrolling and tab frame-state integration checks."""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
from driver import Driver

url = "file:///" + os.path.join(HERE, "scroll-fixture.html").replace("\\", "/")
d = Driver(ephemeral=True, width=800, height=600)
d.navigate(url)
d.wait_load(contains="scroll-fixture")
d.wait_ready()
w, h, pixels = d.screenshot()
assert (w, h) == (800, 600), (w, h)
assert d.eval_js("innerWidth") == 800
assert d.eval_js("innerHeight") == 600
print("viewport flags honored: 800x600")

patches = d.native_frame(format="png", force=True)
assert patches[0][1] == (0, 0, 800, 600)
d.native_frame(format="png")
d.wheel(-300, x=100, y=100)
time.sleep(0.8)
assert d.eval_js("scrollY") > 0, d.eval_js("scrollY")
patches = d.native_frame(format="png")
assert patches
print("wheel scroll:", d.eval_js("scrollY"), "patch bytes:", sum(len(p[4]) for p in patches))

# Native frames must not be diffed across different documents, even at
# exactly the same viewport size.
d.navigate(url + "?second")
d.wait_load(contains="second")
d.wait_ready()
patches = d.native_frame(format="png")
assert patches and patches[0][3] == 1, "navigation did not force a fresh frame"
print("navigation resets native frame state")

# New tabs must have independent native references too.
d.new_tab(1)
d.activate(1)
d.navigate(url)
d.wait_load()
d.wait_ready()
patches = d.native_frame(format="png", tab=1, stream=1)
assert patches
print("tab native frame state independent")

print("ALL OK")
d.close()
