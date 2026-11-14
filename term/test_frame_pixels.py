"""Ensure native PNG pixels match the existing raw screenshot API."""
import io
import re
import os
import sys
import time
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
from driver import Driver

d = Driver(ephemeral=True, width=800, height=600)
d.navigate("file:///" + os.path.join(HERE, "native-fixture.html").replace("\\", "/"))
d.wait_load()
d.wait_ready()
time.sleep(0.5)

w, h, bgra = d.screenshot()
png = d.native_frame(format="png", force=True)[0][4]
img = Image.open(io.BytesIO(png)).convert("RGBA")
a = np.frombuffer(img.tobytes(), dtype=np.uint8).reshape(h, w, 4)
b = np.frombuffer(bgra, dtype=np.uint8).reshape(h, w, 4)[:, :, [2, 1, 0, 3]]
assert np.array_equal(a, b), f"pixel mismatch: {np.sum(a != b)} values"
print("native PNG matches BGRA screenshot exactly")

sixel = d.native_frame(format="sixel", force=True)[0][4]
assert sixel.startswith(b"\x1bP7;1;0q")
print("Sixel forced frame bytes:", len(sixel))
# Same encoder used repeatedly must be deterministic.
sixel2 = d.native_frame(format="sixel", force=True)[0][4]
assert sixel == sixel2
print("Sixel deterministic")


def decode_sixel(data, width, height):
    """Minimal independent decoder for our 8-bit palette, RLE Sixel stream."""
    body = data[data.index(b"q") + 1:-2].decode("ascii")
    if body.startswith('"'):
        end = body.index("#") if "#" in body else len(body)
        body = body[end:]
    palette = {}
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    color = 0
    x = band = 0
    i = 0
    while i < len(body):
        c = body[i]
        if c == "#":
            match = re.match(r"#(\d+)(?:;2;(\d+);(\d+);(\d+))?", body[i:])
            color = int(match.group(1))
            if match.group(2) is not None:
                palette[color] = tuple(round(int(match.group(n)) * 255 / 100) for n in (2, 3, 4))
            i += match.end()
        elif c == "!":
            j = i + 1
            while j < len(body) and body[j].isdigit():
                j += 1
            count = int(body[i + 1:j])
            value = ord(body[j]) - 63
            for _ in range(count):
                for row in range(6):
                    if value & (1 << row) and band + row < height and x < width:
                        canvas[band + row, x] = palette[color]
                x += 1
            i = j + 1
        elif c == "?":
            i += 1
        elif 63 <= ord(c) <= 126:
            value = ord(c) - 63
            for row in range(6):
                if value & (1 << row) and band + row < height and x < width:
                    canvas[band + row, x] = palette[color]
            x += 1
            i += 1
        elif c == "$":
            x = 0
            i += 1
        elif c == "-":
            x = 0
            band += 6
            i += 1
        else:
            i += 1
    return canvas


decoded = decode_sixel(sixel, w, h)
reference = b[:, :, :3]
# Native Sixel is quantized, but large page areas must preserve their color.
assert np.max(np.abs(decoded[10:60, 10:60].astype(int) - reference[10:60, 10:60].astype(int))) <= 8
dark = reference[150:230, 70:250]
assert np.mean(np.abs(decoded[150:230, 70:250].astype(int) - dark.astype(int))) < 20
print("Sixel decoded pixels match page colors within quantization")

d.close()
