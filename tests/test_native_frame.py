"""Integration test for native FRM2 PNG/Sixel patches and benchmark."""
import functools
import io
import os
import sys
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
HERE = os.path.join(os.path.dirname(HERE), "term")
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
from driver import Driver

def serve_fixtures():
    """Serve term/ locally: a live page changes under the test and the
    static-skip assertion becomes a coin flip."""
    handler = functools.partial(SimpleHTTPRequestHandler, directory=HERE)
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}"


BASE = serve_fixtures()
url = sys.argv[1] if len(sys.argv) > 1 else BASE + "/native-fixture.html"
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
# Tile geometry: with a 20px cell grid the Sixel tile height is
# lcm(6, 20) = 60; a full frame is still the whole viewport, but the
# CLIENT quantizes its viewport, and patches are 60px super-rows.
assert sixel[0][1] == (0, 0, 1200, 760), sixel[0][1]
assert len(sixel[0][4]) > 1000, "full sixel frame collapsed to a single patch"
assert sixel[0][4].startswith(b"\x1bP7;1;0q") and sixel[0][4].endswith(b"\x1b\\")
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
d.navigate(BASE + "/term-fixture.html")
d.wait_load(contains="term-fixture.html")
d.wait_ready()
patches = d.native_frame(format="png", force=True)
assert patches and patches[0][1] == (0, 0, 1200, 760)
title = d.eval_js("document.title")
assert title == "Terminal Interaction", title

# Offline regression: a whole batch already queued when native_frame wakes
# (TCP coalescing) must be consumed completely. The old consumer removed
# items while iterating and dropped the packet right after each hit.
class _Batched:
    pass


fake = _Batched()
fake._cond = threading.Condition()
fake._next_id = 500
fake._id_lock = threading.Lock()
fake._alloc_id = Driver._alloc_id.__get__(fake)
fake.dead = False
fake.native_frames = []


def _fake_cmd(obj):
    rb = obj["id"].to_bytes(8, "little")
    fake.native_frames = [
        (rb, (0, 0, 10, 10), 1, 0, b"p1"),
        (rb, (10, 0, 10, 10), 1, 0, b"p2"),
        (rb, (20, 0, 10, 10), 1, 0, b"p3"),
        (rb, (0, 0, 0, 0), 0, 2, b""),
    ]


fake.cmd = _fake_cmd
batched = Driver.native_frame(fake, format="png", timeout=2)
assert [p[4] for p in batched] == [b"p1", b"p2", b"p3"], batched
assert fake.native_frames == [], fake.native_frames
print("native_frame consumes a coalesced multi-patch batch")

print("ALL OK")
d.close()
