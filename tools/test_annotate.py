"""Annotated screenshot test (SDK level).

Checks that screenshot_annotated boxes and numbers every interactive
element, returns an item list keyed by ref, actually writes pixels, and
produces a valid PNG on disk.
"""

import os as _os
TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
ROOT = _os.path.dirname(TOOLS_DIR)
import functools
import http.server
import sys
import threading
import zlib


def load_png_rows(path):
    """Decode a PNG written by save_png (every scanline uses filter 0)."""
    data = open(path, "rb").read()
    pos, idat, w, h = 8, b"", 0, 0
    while pos < len(data):
        ln = int.from_bytes(data[pos:pos + 4], "big")
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        if kind == b"IHDR":
            w = int.from_bytes(body[0:4], "big")
            h = int.from_bytes(body[4:8], "big")
        elif kind == b"IDAT":
            idat += body
        pos += 12 + ln
    raw = zlib.decompress(idat)
    stride = w * 4
    return w, h, [raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)]
                  for y in range(h)]

sys.path.insert(0, TOOLS_DIR)
from driver import Driver

PAGE_URL = "http://127.0.0.1:8136/annotate-test.html"
OUT = TOOLS_DIR + "/annotate-shot.png"

PAGE = """<!doctype html><html><head><title>Annotate</title></head><body>
<h1>Settings</h1>
<label for="q">Search</label>
<input id="q" name="q" placeholder="search">
<label for="n">Number</label>
<input id="n" name="n" type="number">
<label for="c">Country</label>
<select id="c" name="c"><option value="hu">Hungary</option>
<option value="us">United States</option></select>
<button type="button" id="b1">Save</button>
<button type="button" id="b2">Cancel</button>
</body></html>"""

with open(TOOLS_DIR + "/annotate-test.html", "w") as f:
    f.write(PAGE)

handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=TOOLS_DIR)
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8136), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

d = Driver(log=f"{ROOT}/engine-annotate-test.log")
try:
    d.navigate(PAGE_URL)
    d.wait_load(contains="annotate-test.html")
    d.wait_ready()

    r = d.read_structure()
    want = {i["ref"] for i in r["inputs"]} | {s["ref"] for s in r["selects"]} \
        | {b["ref"] for b in r["buttons"]}
    assert want, "page has no interactive elements"

    res = d.screenshot_annotated(OUT)
    assert res["w"] > 0 and res["h"] > 0, res
    assert res["items"], "no annotated items"
    got = {i["ref"] for i in res["items"]}
    assert got == want, f"refs mismatch: drawn {sorted(got)} vs read {sorted(want)}"
    print("annotated refs OK:", sorted(got, key=lambda s: int(s[1:])))

    # Every item carries a label an agent can quote to a human.
    labels = {i["label"] for i in res["items"]}
    assert {"Search", "Number", "Country", "Save", "Cancel"} <= labels, labels
    print("labels OK:", sorted(labels))

    # Pixels were actually written, and the PNG exists with real content.
    assert res["pixels_drawn"] > 500, res["pixels_drawn"]
    size = _os.path.getsize(OUT)
    assert size > 2000, size
    with open(OUT, "rb") as f:
        head = f.read(8)
    assert head == b"\x89PNG\r\n\x1a\n", head
    print("png OK:", size, "bytes,", res["pixels_drawn"], "pixels drawn")

    # Numbers map back to the same refs (num == ref suffix).
    for it in res["items"]:
        assert it["num"] == int(it["ref"][1:]), it
    print("numbering OK")

    # Pixel-accurate placement: the box corner carries the rect color and
    # the label area holds white glyph pixels. save_png wrote BGRA as RGBA,
    # so the rect color (0,80,255,255) lands as (255,80,0,255).
    pw, ph, rows = load_png_rows(OUT)
    assert (pw, ph) == (res["w"], res["h"]), (pw, ph, res["w"], res["h"])
    rect_rgba = bytes((255, 80, 0, 255))
    checked = 0
    for it in res["items"]:
        x, y = it["x"], it["y"]
        if x < 0 or y < 0 or x + 2 >= pw or y + 2 >= ph:
            continue
        assert rows[y][x * 4:(x + 1) * 4] == rect_rgba, \
            f"box corner not drawn at {it['ref']} ({x},{y}): " \
            f"{rows[y][x * 4:(x + 1) * 4]}"
        whites = 0
        for yy in range(y + 2, min(y + 14, ph)):
            for xx in range(x + 2, min(x + 14, pw)):
                px = rows[yy][xx * 4:(xx + 1) * 4]
                if px == b"\xff\xff\xff\xff":
                    whites += 1
        assert whites >= 8, f"no glyph pixels for {it['ref']}: {whites}"
        checked += 1
    assert checked >= 3, checked
    print("pixel placement OK:", checked, "boxes verified")

    print("ALL OK")
finally:
    d.close()
    try:
        _os.remove(OUT)
    except OSError:
        pass