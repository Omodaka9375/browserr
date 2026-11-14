"""Integration test for native FRM2 PNG/Sixel patches and benchmark."""
import io
import os
import sys
import time
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
from driver import Driver

url = sys.argv[1] if len(sys.argv) > 1 else "https://browserr.dev/"
d = Driver(ephemeral=True, width=1200, height=760)
d.navigate(url)
d.wait_load()
d.wait_ready()
time.sleep(1)


def timed(label, fn):
    start = time.perf_counter()
    result = fn()
    ms = (time.perf_counter() - start) * 1000
    print(f"{label:40s} {ms:8.1f} ms")
    return result


def wait_static(format):
    # A live page may finish loading images or animate. The protocol must
    # eventually return no patches; it must not blindly redraw each time.
    for _ in range(12):
        patches = timed(f"native {format} settle", lambda: d.native_frame(format=format))
        if not patches:
            return
        time.sleep(0.5)
    raise AssertionError(f"page never became static ({format})")


patches = timed("native PNG full frame", lambda: d.native_frame(format="png", force=True))
assert len(patches) == 1
_, rect, fmt, flags, png = patches[0]
assert rect == (0, 0, 1200, 760)
assert fmt == 1 and flags == 1
im = Image.open(io.BytesIO(png))
assert im.size == (1200, 760)
im.save(os.path.join(HERE, "native-frame.png"))
print("PNG verified:", len(png), "bytes")

wait_static("png")

# Add a known dirty area, then verify only the affected terminal cell rows.
d.eval_js("""(() => {
  let e = document.createElement('div');
  e.id = 'native-patch';
  e.style.cssText = 'position:fixed;left:8px;top:8px;width:160px;height:48px;background:#f0a;color:#fff;z-index:99999';
  e.textContent = 'PATCH';
  document.body.appendChild(e);
})()""")
time.sleep(0.5)
patches = timed("native PNG small DOM patch", lambda: d.native_frame(format="png"))
assert patches
for _, rect, fmt, flags, payload in patches:
    assert fmt == 1 and flags == 0
    assert rect[0] % 10 == 0 and rect[1] % 20 == 0
    assert rect[2] < 400 and rect[3] < 300, rect
    Image.open(io.BytesIO(payload)).verify()
print("patch rectangles:", [p[1] for p in patches])
print("patch payload bytes:", sum(len(p[4]) for p in patches))

wait_static("png")

sixel = timed("native Sixel full frame", lambda: d.native_frame(format="sixel", force=True))
assert len(sixel) == 1 and sixel[0][2] == 2
assert sixel[0][1] == (0, 0, 1200, 760), sixel[0][1]
assert len(sixel[0][4]) > 1000, "full sixel frame collapsed to a single patch"
assert sixel[0][4].startswith(b"\x1bP0;1;0q") and sixel[0][4].endswith(b"\x1b\\")
print("Sixel bytes:", sum(len(p[4]) for p in sixel))
wait_static("sixel")

# A one-line mutation should produce terminal-aligned Sixel patches, not
# another full-screen transmission.
d.eval_js("document.getElementById('native-patch').textContent = 'UPDATED'")
time.sleep(0.5)
patch = timed("native Sixel small DOM patch", lambda: d.native_frame(format="sixel"))
assert patch and all(p[1][2] < 400 for p in patch), [p[1] for p in patch]
print("Sixel patch:", [p[1] for p in patch], sum(len(p[4]) for p in patch), "bytes")

# Interaction remains functional on this same engine/session.
d.navigate("https://example.com")
d.wait_load(contains="example.com")
d.wait_ready()
patches = d.native_frame(format="png", force=True)
assert patches and patches[0][1] == (0, 0, 1200, 760)
title = d.eval_js("document.title")
assert title == "Example Domain", title

print("ALL OK")
d.close()
