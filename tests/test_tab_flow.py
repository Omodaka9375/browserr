"""Multi-tab TUI behavior: switching repaints and the status follows the tab."""
import os
import pathlib
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HERE = os.path.join(os.path.dirname(HERE), "term")
for p in (HERE, os.path.join(HERE, "..", "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)
from driver import Driver
from browserr_term import Term

fixture = pathlib.Path(os.path.join(HERE, "native-fixture.html")).as_uri()
d = Driver(ephemeral=True)
t = Term(d, 100, 30)

# Tab 0
t.go(fixture)
d.wait_load()
d.wait_ready()

# Create tab 1 exactly like ctrl+t does.
nid = max(t.tabs) + 1
d.new_tab(nid)
d.activate(nid)
t.tabs.append(nid)
t.active = nid
t.force_next = True
d.navigate("about:blank", tab=nid)
time.sleep(0.8)

# Tab 1 frame stream must produce a FULL frame, not a diff against tab 0.
patches = d.native_frame(format="png", tab=nid, stream=nid, force=False)
assert patches and patches[0][3] & 1, "tab 1 must begin with a full frame"

# Navigate tab 1 to a real second document, then poll engine events like
# the TUI does.
fixture2 = pathlib.Path(os.path.join(HERE, "term-fixture.html")).as_uri()
d.navigate(fixture2, tab=nid)
d.wait_load(tab=nid)
d.wait_ready(tab=nid)
time.sleep(0.3)
t.poll_meta()
assert t.url == fixture2, t.url
assert t.title == "Terminal Interaction", t.title
print("tab 1 meta follows its own url/title")

# Switch back to tab 0: forced full frame again, and status returns to the
# fixture URL without any navigation.
t.cycle_tab(-1)
assert t.active == 0 and t.force_next
t.poll_meta()
assert t.url == fixture, t.url
patches = d.native_frame(format="png", tab=0, stream=0, force=True)
assert patches and patches[0][3] & 1
print("tab 0 switch forces full frame and restores url")

print("ALL OK")
d.close()
