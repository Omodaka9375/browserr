"""Pixel annotation helpers for evidence screenshots (no font/PIL
dependency): PNG encoding of BGRA buffers plus 3x5-glyph number and
rectangle drawing. Split out of driver.py; driver.py re-exports
save_png so `from driver import save_png` keeps working."""
import struct
import zlib

def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")


def save_png(path: str, w: int, h: int, bgra: bytes) -> None:
    """Write a BGRA byte buffer as PNG (converted to RGBA)."""
    rgba = bytearray()
    for i in range(0, len(bgra), 4):
        rgba += bytes((bgra[i + 2], bgra[i + 1], bgra[i], bgra[i + 3]))
    stride = w * 4
    scanlines = b"".join(b"\x00" + bytes(rgba[y * stride:(y + 1) * stride]) for y in range(h))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    body = (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(scanlines, 6))
            + _png_chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(body)


# 3x5 digit glyphs: screenshot annotation with no font dependency.
_DIGITS = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
}


def _put(buf, w: int, h: int, x: int, y: int, color) -> int:
    """Write one BGRA pixel if it lands inside the buffer."""
    if 0 <= x < w and 0 <= y < h:
        i = (y * w + x) * 4
        buf[i] = color[0]
        buf[i + 1] = color[1]
        buf[i + 2] = color[2]
        buf[i + 3] = color[3]
        return 1
    return 0


def draw_rect(buf, w: int, h: int, x, y, rw, rh,
              color=(0, 80, 255, 255), thick=2) -> int:
    """Outlined rectangle in BGRA order. Returns pixels written."""
    n = 0
    x0, y0 = int(x), int(y)
    x1, y1 = int(x + rw), int(y + rh)
    for t in range(thick):
        for xx in range(x0, x1 + 1):
            n += _put(buf, w, h, xx, y0 + t, color)
            n += _put(buf, w, h, xx, y1 - t, color)
        for yy in range(y0, y1 + 1):
            n += _put(buf, w, h, x0 + t, yy, color)
            n += _put(buf, w, h, x1 - t, yy, color)
    return n


def draw_number(buf, w: int, h: int, x, y, num, scale=2,
                fg=(255, 255, 255, 255), bg=(20, 20, 20, 255)) -> int:
    """Draw a decimal number with the built-in 3x5 font. Returns pixels."""
    s = str(int(num))
    n = 0
    pad = 1
    box_w = len(s) * (3 * scale + scale) + pad * 2
    box_h = 5 * scale + pad * 2
    for yy in range(int(y), int(y) + box_h):
        for xx in range(int(x), int(x) + box_w):
            n += _put(buf, w, h, xx, yy, bg)
    cx = int(x) + pad
    for ch in s:
        glyph = _DIGITS.get(ch)
        if glyph:
            for row, bits in enumerate(glyph):
                for col, bit in enumerate(bits):
                    if bit == "1":
                        for dy in range(scale):
                            for dx in range(scale):
                                n += _put(buf, w, h, cx + col * scale + dx,
                                          int(y) + pad + row * scale + dy, fg)
        cx += 3 * scale + scale
    return n
