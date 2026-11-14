#!/usr/bin/env python3
"""Browserr Term: render the Browserr engine inside a terminal pane.

Interactive terminal renderer, backed by native Rust frame extraction.

Native FRM2 patches avoid the old 3.6MB screenshot transfer and Python
quantizer: Servo RGBA is compared and encoded in the engine, and only dirty
terminal-aligned rectangles cross the socket. Sixel patches are emitted at
their native cell coordinates; PNG is available to other native clients.

Renderers:
  half   (default) 2 pixels per cell via U+2580 + truecolor; DIRTY-CELL
         diffing: only cells whose color changed since the last frame are
         re-emitted, as cursor-positioned runs. A static page emits nothing;
         a small DOM update emits a few dozen bytes. This is the "tiny dirt
         patches" mode and works in every terminal incl. Windows Terminal.
  kitty  native PNG patches composed into a full-resolution image for the
         kitty graphics protocol (ghostty/kitty/iTerm2/wezterm).
  sixel  native terminal-ready patches at native resolution, with Rust
         quantization and dirty rectangles (Windows Terminal 1.22+).

Keys: ctrl+l url  ctrl+t new tab  ctrl+] next  ctrl+[ prev  ctrl+r reload
       j/k/u/d/g/G scroll  arrows/PageUp/PageDown scroll  ctrl+q quit
Mouse: SGR tracking; click = page click, wheel = page scroll.

Self-test (no tty needed):
  python browserr_term.py --self-test http://127.0.0.1:8160/
"""

import os
import sys
import time
import struct
import io
import base64

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (os.path.join(HERE), os.path.join(HERE, "..", "tools"),
          os.path.join(HERE, "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

from driver import Driver  # noqa: E402

try:
    import numpy as np
except ImportError:
    np = None

try:
    from PIL import Image
except ImportError:
    Image = None
ESC = "\x1b"
RESET = "\x1b[0m"


# ---------------------------------------------------------------- input

# Windows virtual keys that arrive with no UnicodeChar still need to
# reach parse_keys; translate them to the CSI forms it understands.
VK_CSI = {
    0x21: b"\x1b[5~", 0x22: b"\x1b[6~", 0x23: b"\x1b[H", 0x24: b"\x1b[F",
    0x25: b"\x1b[D", 0x26: b"\x1b[A", 0x27: b"\x1b[C", 0x28: b"\x1b[B",
    0x2D: b"\x1b[2~", 0x2E: b"\x1b[3~",
}


def win_key_bytes(vk, char_code, state):
    """Translate a console KEY_EVENT_RECORD to parse_keys byte streams.

    Ctrl+letter combos frequently arrive as the printable character with
    a control-key flag instead of the control code; both consoles and
    Windows Terminal do this depending on mode. Returns None to drop.
    """
    if char_code == 0:
        return VK_CSI.get(vk)
    # Ctrl without Alt (Ctrl+Alt is AltGr, a layout modifier, not control).
    ctrl = (state & 0x000C) and not (state & 0x0003)
    if ctrl and char_code < 0x80:
        return bytes([char_code & 0x1F])
    return chr(char_code).encode("utf-8")


class RawInput:
    """Raw console/terminal bytes, with real mouse support on Windows.

    msvcrt sees typed characters but not console mouse records. Windows
    Terminal delivers mouse input through ReadConsoleInputW, so the input
    loop translates those records to the same SGR sequences used on POSIX.
    """

    def __init__(self):
        self.win = os.name == "nt"
        self.old = None
        self.handle = None
        if self.win:
            import ctypes
            k = ctypes.windll.kernel32
            self.handle = k.GetStdHandle(-10)
            self.old = ctypes.c_uint32()
            k.GetConsoleMode(self.handle, ctypes.byref(self.old))
        else:
            import termios
            import tty
            self.termios, self.tty = termios, tty

    def __enter__(self):
        if self.win:
            import ctypes
            # Window input + mouse, no line buffering, echo, or cooked edit.
            ctypes.windll.kernel32.SetConsoleMode(self.handle, 0x0018)
        else:
            self.old = self.termios.tcgetattr(sys.stdin.fileno())
            self.tty.setcbreak(sys.stdin.fileno())
        return self

    def __exit__(self, *a):
        if self.win:
            import ctypes
            ctypes.windll.kernel32.SetConsoleMode(self.handle, self.old)
        elif self.old is not None:
            self.termios.tcsetattr(sys.stdin.fileno(), self.termios.TCSAFLUSH,
                                    self.old)

    def read(self, timeout=0.0):
        if self.win:
            return self._read_windows(timeout)
        import select
        r, _, _ = select.select([sys.stdin], [], [], timeout or 0)
        if not r:
            return b""
        return os.read(sys.stdin.fileno(), 4096)

    def _read_windows(self, timeout):
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        u = ctypes.wintypes

        class Coord(ctypes.Structure):
            _fields_ = [("X", u.SHORT), ("Y", u.SHORT)]

        class CharUnion(ctypes.Union):
            _fields_ = [("UnicodeChar", u.WCHAR), ("AsciiChar", u.CHAR)]

        class KeyEvent(ctypes.Structure):
            _fields_ = [("bKeyDown", u.BOOL), ("wRepeatCount", u.WORD),
                        ("wVirtualKeyCode", u.WORD), ("wVirtualScanCode", u.WORD),
                        ("uChar", CharUnion), ("dwControlKeyState", u.DWORD)]

        class MouseEvent(ctypes.Structure):
            _fields_ = [("dwMousePosition", Coord), ("dwButtonState", u.DWORD),
                        ("dwControlKeyState", u.DWORD), ("dwEventFlags", u.DWORD)]

        class EventUnion(ctypes.Union):
            _fields_ = [("KeyEvent", KeyEvent), ("MouseEvent", MouseEvent)]

        class InputRecord(ctypes.Structure):
            _fields_ = [("EventType", u.WORD), ("Event", EventUnion)]

        assert ctypes.sizeof(Coord) == 4
        assert ctypes.sizeof(MouseEvent) == 16
        assert ctypes.sizeof(InputRecord) == 20
        end = time.time() + timeout
        result = bytearray()
        while time.time() < end:
            count = u.DWORD()
            if not k.GetNumberOfConsoleInputEvents(self.handle, ctypes.byref(count)):
                return bytes(result)
            if not count.value:
                if result:
                    break
                time.sleep(0.003)
                continue
            records = (InputRecord * min(count.value, 128))()
            read = u.DWORD()
            if not k.ReadConsoleInputW(self.handle, records, len(records), ctypes.byref(read)):
                break
            for rec in records[:read.value]:
                if rec.EventType == 2:
                    e = rec.Event.MouseEvent
                    x, y = e.dwMousePosition.X + 1, e.dwMousePosition.Y + 1
                    if e.dwEventFlags & 4:
                        delta = (e.dwButtonState >> 16) & 0xFFFF
                        if delta >= 0x8000:
                            delta -= 0x10000
                        cb = 64 if delta > 0 else 65
                        result += f"\x1b[<{cb};{x};{y}M".encode()
                    elif e.dwEventFlags & 1:
                        result += f"\x1b[<32;{x};{y}M".encode()
                    elif e.dwButtonState & 1:
                        result += f"\x1b[<0;{x};{y}M".encode()
                    else:
                        result += f"\x1b[<3;{x};{y}m".encode()
                elif rec.EventType == 1 and rec.Event.KeyEvent.bKeyDown:
                    e = rec.Event.KeyEvent
                    data = win_key_bytes(e.wVirtualKeyCode,
                                         ord(e.uChar.UnicodeChar),
                                         e.dwControlKeyState)
                    if data:
                        result += data
        return bytes(result)


def parse_keys(buf):
    """Split a stdin byte buffer into key events.

    Returns (events, rest). Event: dict(kind='key'|'ctrl'|'text') or
    dict(kind='mouse', action='press'|'drag'|'release'|'wheel', ...).
    """
    events = []
    i = 0
    n = len(buf)
    while i < n:
        b = buf[i]
        if b == 0x1B:
            if buf[i:i + 3] == b"\x1b[<":
                j = buf.find(b"M", i)
                k = buf.find(b"m", i)
                ends = [x for x in (j, k) if x != -1]
                if not ends:
                    break  # incomplete
                end = min(ends)
                params = buf[i + 3:end].decode("ascii", "replace")
                final = chr(buf[end])
                try:
                    parts = params.split(";")
                    cb = int(parts[0])
                    px = int(parts[1])
                    py = int(parts[2])
                except (ValueError, IndexError):
                    i = end + 1
                    continue
                motion = cb & 32
                if cb & 64:
                    # SGR wheel: 64 = up, 65 = down
                    events.append(dict(kind="mouse", action="wheel",
                                       dx=-1 if (cb & 1) == 0 else 1,
                                       x=px, y=py))
                elif cb & 3 == 3:
                    events.append(dict(kind="mouse", action="release",
                                       btn=None, x=px, y=py))
                else:
                    events.append(dict(kind="mouse",
                                       action="drag" if motion else "press",
                                       btn=cb & 3, x=px, y=py))
                i = end + 1
                continue
            if buf[i:i + 2] == b"\x1b[":
                j = i + 2
                while j < n and 0x30 <= buf[j] <= 0x3F:
                    j += 1
                if j < n and 0x40 <= buf[j] <= 0x7E:
                    seq = buf[i:j + 1].decode("ascii", "replace")
                    ev = _csi_key(seq)
                    if ev:
                        events.append(ev)
                    i = j + 1
                    continue
                i += 2
                continue
            if buf[i:i + 2] == b"\x1bO" and i + 3 <= n:
                seq = buf[i:i + 3].decode("ascii", "replace")
                ev = _csi_key(seq)
                if ev:
                    events.append(ev)
                i += 3
                continue
            events.append(dict(kind="key", name="Escape", text=""))
            i += 1
            continue
        if b in (0x0D, 0x0A):
            events.append(dict(kind="key", name="Enter", text=""))
        elif b in (0x08, 0x7F):
            events.append(dict(kind="key", name="Backspace", text=""))
        elif b == 0x09:
            events.append(dict(kind="key", name="Tab", text=""))
        elif b < 0x20:
            events.append(dict(kind="ctrl", c=chr(b + 0x40)))
        else:
            if b < 0x80:
                events.append(dict(kind="text", text=chr(b)))
                i += 1
                continue
            ln = 2 if b < 0xE0 else 3 if b < 0xF0 else 4
            if i + ln > n:
                break
            try:
                events.append(dict(kind="text", text=buf[i:i + ln].decode("utf-8")))
            except UnicodeDecodeError:
                pass
            i += ln
            continue
        i += 1
    return events, buf[i:]


def _csi_key(seq):
    m = {
        "\x1b[A": "Up", "\x1b[B": "Down", "\x1b[C": "Right", "\x1b[D": "Left",
        "\x1bOA": "Up", "\x1bOB": "Down", "\x1bOC": "Right", "\x1bOD": "Left",
        "\x1b[H": "Home", "\x1b[F": "End", "\x1b[1~": "Home", "\x1b[4~": "End",
        "\x1b[5~": "PageUp", "\x1b[6~": "PageDown", "\x1b[3~": "Delete",
    }
    name = m.get(seq)
    return dict(kind="key", name=name, text="") if name else None


# ------------------------------------------------------- dirty renderer

def to_rgb(img_bgra, w, h):
    a = np.frombuffer(bytes(img_bgra[:w * h * 4]), dtype=np.uint8).reshape(h, w, 4)
    return a[:, :, :3][:, :, ::-1].copy()


class HalfblockDirty:
    """Dirty-cell half-block renderer.

    Emits only cells whose (top,bottom) color pair changed. Static pages
    emit zero bytes; small updates emit a handful of positioned runs.
    """

    def __init__(self, cols, view_rows, out):
        self.cols, self.view_rows, self.out = cols, view_rows, out
        self.prev = None  # (view_rows, cols, 6) uint8

    def resize(self, cols, view_rows):
        self.cols, self.view_rows = cols, view_rows
        self.prev = None

    def sample(self, img):
        th = self.view_rows * 2
        ys = (np.arange(th) * img.shape[0] // th).clip(0, img.shape[0] - 1)
        xs = (np.arange(self.cols) * img.shape[1] // self.cols).clip(0, img.shape[1] - 1)
        small = img[ys][:, xs]
        top = small[0::2]
        bot = small[1::2]
        cur = np.concatenate([top, bot], axis=2)  # rows, cols, 6
        return top, bot, cur

    def render(self, img, status_fn=None):
        top, bot, cur = self.sample(img)
        if self.prev is None or self.prev.shape != cur.shape:
            mask = np.ones(cur.shape[:2], dtype=bool)
        else:
            mask = np.any(cur != self.prev, axis=2)
        self.prev = cur
        out = self.out
        for y in range(self.view_rows):
            xs = np.nonzero(mask[y])[0]
            if xs.size == 0:
                continue
            splits = np.where(np.diff(xs) > 1)[0]
            for g in np.split(xs, splits + 1):
                out.write(f"{ESC}[{y + 1};{g[0] + 1}H")
                last = None
                for c in g:
                    f = top[y, c]
                    b = bot[y, c]
                    key = (int(f[0]), int(f[1]), int(f[2]),
                           int(b[0]), int(b[1]), int(b[2]))
                    if key != last:
                        out.write(f"{ESC}[38;2;{key[0]};{key[1]};{key[2]}m"
                                  f"{ESC}[48;2;{key[3]};{key[4]};{key[5]}m")
                        last = key
                    out.write("\u2580")
        if status_fn:
            status_fn()
        out.flush()
        return int(mask.sum())


class NativeKitty:
    """Compose native PNG patches and emit the image through Kitty.

    Kitty has no universal sub-rectangle replacement primitive, so patches
    update a canvas and only a changed composite is retransmitted. PNG
    decoding/encode stays out of the frame transfer and dirty detection.
    """

    def __init__(self, out, driver, cols, view_rows):
        self.out = out
        self.driver = driver
        self.canvas = None
        self.cols = cols
        self.view_rows = view_rows
        self.last_ms = 0.0

    def render(self, patches):
        if not patches:
            return 0
        t0 = time.time()
        if self.canvas is None:
            from PIL import Image
            self.canvas = Image.new("RGBA", (self.driver.width, self.driver.height), "white")
        from PIL import Image
        for _, (x, y, w, h), fmt, _, payload in patches:
            if fmt != 1:
                continue
            patch = Image.open(io.BytesIO(payload)).convert("RGBA")
            self.canvas.paste(patch, (x, y))
        encoded = io.BytesIO()
        self.canvas.save(encoded, format="PNG", optimize=False, compress_level=1)
        data = base64.b64encode(encoded.getvalue())
        for start in range(0, len(data), 4096):
            chunk = data[start:start + 4096]
            more = 1 if start + 4096 < len(data) else 0
            self.out.write(f"\x1b_Ga=T,f=100,i=7,q=2,m={more};".encode()
                           + chunk + b"\x1b\\")
        self.out.flush()
        self.last_ms = (time.time() - t0) * 1000
        return len(patches)


class NativeSixel:
    """Render engine-produced Sixel patches directly at native cell coords.

    No raw screenshots, Python quantization, or PNG work is involved. The
    engine aligns dirty rectangles to the terminal's cell grid; each packet
    becomes one cursor-positioned Sixel write. The end packet carries no
    pixels and therefore no redraw.

    Inside tmux, image escapes from an application are dropped unless
    wrapped in tmux's DCS passthrough envelope (allow-passthrough on).
    """

    def __init__(self, out, cell):
        self.out = out
        self.cell = cell
        self.last_ms = 0.0
        self.wrap = bool(os.environ.get("TMUX"))
        self._wrap_open = f"{ESC}Ptmux;".encode()
        self._wrap_close = b"\x1b\\"

    def render(self, patches):
        t0 = time.time()
        for _, (x, y, w, h), fmt, _, payload in patches:
            if fmt != 2 or not payload:
                continue
            cx = x // self.cell[0] + 1
            cy = y // self.cell[1] + 1
            data = f"\x1b[{cy};{cx}H".encode() + payload
            if self.wrap:
                data = self._wrap_open + data + self._wrap_close
            self.out.write(data)
        self.out.flush()
        self.last_ms = (time.time() - t0) * 1000
        return len(patches)


# ---------------------------------------------------------------- main

class Term:
    def __init__(self, driver, cols, rows, out=sys.stdout):
        self.d = driver
        self.cols, self.rows = cols, rows
        self.out = out
        # One row for URL/status and one for key help. Graphics never enter
        # these rows; bottom-edge terminal scrolling would otherwise smear
        # the Sixel image and detach patch coordinates from the screen.
        self.view_rows = max(1, rows - 2)
        self.url_mode = False
        self.url_buf = ""
        self.status = "starting"
        self.title = ""
        self.url = ""
        self.kitty = False
        self.sixel = False
        self.renderer = None
        self.running = True
        self.tabs = [0]
        self.active = 0
        self.dirty = 0
        self.frames = 0
        self.fps_now = 0.0
        self.last_frame_ms = 0.0
        self.force_next = False     # full frame on next render (tab switch)
        self._meta_seq = 0         # event cursor for url/title updates

    def to_page_xy(self, cx, cy):
        if (self.sixel or self.kitty) and getattr(self, "cell", None):
            cw, ch = self.cell
            px = (cx - 1) * cw
            py = (cy - 1) * ch
        else:
            px = cx * self.d.width / max(1, self.cols)
            py = cy * 2 * self.d.height / max(1, self.view_rows * 2)
        return int(min(self.d.width - 1, max(0, px))), \
            int(min(self.d.height - 1, max(0, py)))

    def poll_meta(self):
        """Consume engine url/title events for the ACTIVE tab.

        The engine pushes these on change; reading them costs no round
        trip, unlike the eval polling this replaces (which also always
        read tab 0, so the status bar never followed a tab switch)."""
        events = self.d.events
        if self._meta_seq > len(events):
            self._meta_seq = 0  # events were cleared externally
        for e in events[self._meta_seq:]:
            if e.get("ev") == "url" and e.get("tab") == self.active:
                self.url = e.get("url") or self.url
            elif e.get("ev") == "title" and e.get("tab") == self.active:
                self.title = (e.get("title") or "")[:40]
        self._meta_seq = len(events)

    def handle_key(self, ev):
        if ev["kind"] == "ctrl":
            c = ev["c"].lower()
            if c == "q":
                self.running = False
            elif c == "l":
                self.url_mode = True
                self.url_buf = ""
            elif c == "t":
                nid = max(self.tabs) + 1
                self.d.new_tab(nid)
                self.d.activate(nid)
                self.tabs.append(nid)
                self.active = nid
                self.d.navigate("about:blank", tab=nid)
                self.force_next = True
            elif c == "r":
                self.d.navigate(self.url or "about:blank", tab=self.active)
                self.status = "reloading"
            elif c == "]":
                self.cycle_tab(1)
            elif c == "[":
                self.cycle_tab(-1)
        elif ev["kind"] == "key":
            n = ev["name"]
            if n == "Escape":
                self.url_mode = False
            elif self.url_mode and n == "Enter":
                url = self.url_buf
                self.url_mode = False
                if url:
                    self.go(url)
            elif n == "Up":
                self.d.wheel(40, tab=self.active)
            elif n == "Down":
                self.d.wheel(-40, tab=self.active)
            elif n == "PageUp":
                self.d.wheel(300, tab=self.active)
            elif n == "PageDown":
                self.d.wheel(-300, tab=self.active)
            elif n == "Home":
                self.d.wheel(10000, tab=self.active)
            elif n == "End":
                self.d.wheel(-10000, tab=self.active)
            elif not self.url_mode:
                self.d.key(n, tab=self.active)
        elif ev["kind"] == "text":
            if self.url_mode:
                self.url_buf += ev["text"]
            else:
                ch = ev["text"]
                self.d.key("text", ch, down=True, tab=self.active)
                self.d.key("text", ch, down=False, tab=self.active)
        if ev["kind"] == "key" and ev.get("name") == "Backspace":
            if self.url_mode:
                self.url_buf = self.url_buf[:-1]
            else:
                self.d.key("Backspace", tab=self.active)

    def cycle_tab(self, d):
        if len(self.tabs) < 2:
            return
        i = (self.tabs.index(self.active) + d) % len(self.tabs)
        self.active = self.tabs[i]
        self.d.activate(self.active)
        # The engine diffs each tab against its own reference; an unchanged
        # tab would produce no patches, leaving the previous tab's pixels on
        # screen. Force one full frame for the newly active tab.
        self.force_next = True
        self._meta_seq = 0  # rescan history for this tab's url/title

    def handle_mouse(self, ev):
        if ev["action"] == "wheel":
            self.d.wheel(-40 * ev["dx"], tab=self.active)
            return
        px, py = self.to_page_xy(ev["x"], ev["y"])
        if ev["action"] == "press":
            self.d.click(px, py, tab=self.active)
        elif ev["action"] == "drag":
            self.d.move(px, py, tab=self.active)

    def go(self, url):
        if not (url.startswith("http://") or url.startswith("https://")
                or url.startswith("file://") or url.startswith("about:")):
            url = "https://" + url
        self.url = url
        self.status = "loading"
        self.d.navigate(url, tab=self.active)
        self.force_next = True

    def draw_status(self):
        mode = "URL" if self.url_mode else "VIEW"
        s = f" [{mode}] {self.url if not self.url_mode else self.url_buf + '_'}"
        s += f" | tab {self.active} | {self.status} | {self.fps_now:.0f}fps"
        s += f" | frame:{self.last_frame_ms:.0f}ms"
        if self.renderer is not None:
            s += f" | {type(self.renderer).__name__} dirty:{self.dirty}"
            ms = getattr(self.renderer, "last_ms", 0.0)
            if ms:
                s += f" write:{ms:.1f}ms"
        url_bar = self.rows - 1
        help_bar = self.rows
        text = (
            f"{ESC}[{url_bar};1H{ESC}[48;2;170;170;170m"
            f"{ESC}[38;2;0;0;0m{s[:self.cols].ljust(self.cols)}{RESET}"
            f"{ESC}[{help_bar};1H{ESC}[48;2;30;30;30m"
            f"{ESC}[38;2;235;235;235m"
            f"{'ctrl+l url  ctrl+t tab  ctrl+]/[ switch  ctrl+q quit'[:self.cols].ljust(self.cols)}{RESET}")
        if hasattr(self.out, "buffer"):
            self.out.buffer.write(text.encode())
            self.out.buffer.flush()
        else:
            self.out.write(text)
            self.out.flush()
        self.out.flush()

    def render_frame(self, w, h, bgra):
        if isinstance(self.renderer, HalfblockDirty):
            self.dirty = self.renderer.render(
                to_rgb(bgra, w, h), status_fn=self.draw_status)
        elif isinstance(self.renderer, (NativeSixel, NativeKitty)):
            self.dirty = self.renderer.render(bgra)
            self.draw_status()
        self.frames += 1


def terminal_size():
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.GetStdHandle(-11)
        csbi = ctypes.create_string_buffer(22)
        if ctypes.windll.kernel32.GetConsoleScreenBufferInfo(h, csbi):
            # CONSOLE_SCREEN_BUFFER_INFO: dwSize, dwCursorPosition,
            # wAttributes, srWindow {Left, Top, Right, Bottom}.
            left, top, right, bottom = struct.unpack_from("<4h", csbi.raw, 10)
            return right - left + 1, bottom - top + 1
        return 120, 34
    import fcntl
    import termios as _t
    s = struct.unpack("HHHH", fcntl.ioctl(sys.stdin.fileno(),
                                          _t.TIOCGWINSZ, b"\x00" * 8))
    return s[1], s[0]


def setup_terminal():
    sys.stdout.write(f"{ESC}[?1049h{ESC}[?25l{ESC}[?1006;1000;1002h")
    sys.stdout.flush()


def teardown_terminal():
    sys.stdout.write(f"{ESC}[?1002l{ESC}[?1000l{ESC}[?25h{ESC}[?1049l")
    sys.stdout.flush()


def query_kitty_support():
    if os.name == "nt":
        return False
    import termios
    import select
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        import tty as _tty
        _tty.setcbreak(fd)
        sys.stdout.write(f"{ESC}[_Gi=31,s=1,v=1,a=q;OK{ESC}\\{ESC}[c")
        sys.stdout.flush()
        deadline = time.time() + 0.3
        buf = b""
        while time.time() < deadline:
            r, _, _ = select.select([sys.stdin], [], [], 0.05)
            if r:
                buf += os.read(fd, 4096)
                if b"i=31" in buf:
                    return True
        return False
    except Exception:
        return False
    finally:
        termios.tcsetattr(fd, termios.TCSAFLUSH, old)


def _query_ansi(payload, terminator, timeout=0.4):
    """Write a terminal query and read the response bytes (or None).
    On Windows the console input is switched to VT input mode for the
    query and restored afterwards (mouse reporting is off at query time).
    """
    if os.name == "nt":
        import ctypes
        import msvcrt
        k = ctypes.windll.kernel32
        h_in = k.GetStdHandle(-10)
        old = ctypes.c_uint32()
        k.GetConsoleMode(h_in, ctypes.byref(old))
        k.SetConsoleMode(h_in, 0x200)  # ENABLE_VIRTUAL_TERMINAL_INPUT
        try:
            sys.stdout.write(payload)
            sys.stdout.flush()
            buf = b""
            end = time.time() + timeout
            while time.time() < end:
                while msvcrt.kbhit():
                    c = msvcrt.getch()
                    if c in (b"\x00", b"\xe0"):
                        c += msvcrt.getch()
                    buf += c
                if buf.endswith(terminator):
                    return buf
                time.sleep(0.01)
            return buf or None
        finally:
            k.SetConsoleMode(h_in, old)
    import termios
    import select
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        import tty as _tty
        _tty.setcbreak(fd)
        # Inside tmux, raw queries never reach the outer terminal: tmux
        # answers some itself and drops the rest, so cell-size detection
        # silently falls back to a wrong default and every patch lands in
        # the wrong cell. Wrap the query in tmux's passthrough envelope
        # (requires allow-passthrough on; the reply still reaches stdin).
        if os.environ.get("TMUX"):
            payload = f"{ESC}Ptmux;{payload}{ESC}\\"
        sys.stdout.write(payload)
        sys.stdout.flush()
        buf = b""
        end = time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([sys.stdin], [], [], 0.05)
            if r:
                buf += os.read(fd, 4096)
            if buf.endswith(terminator):
                return buf
        return buf or None
    finally:
        termios.tcsetattr(fd, termios.TCSAFLUSH, old)


def query_sixel_support():
    """DA1 (ESC[c): sixel support is param 4 in the response."""
    try:
        r = _query_ansi("\x1b[c", b"c")
    except Exception:
        return False
    if not r or not r.startswith(b"\x1b[?") or not r.endswith(b"c"):
        return False
    params = r[3:-1].split(b";")
    return any(p.lstrip(b"?") == b"4" for p in params)


def parse_cell_size_reply(reply):
    """Parse the CSI 16 t reply: CSI 6 ; height ; width t."""
    if not reply or not reply.startswith(b"\x1b[6;") or not reply.endswith(b"t"):
        return None
    nums = reply[4:-1].split(b";")
    if len(nums) != 2:
        return None
    try:
        h, w = int(nums[0]), int(nums[1])
        if w <= 0 or h <= 0 or w > 64 or h > 128:
            return None
        return w, h
    except ValueError:
        return None


def query_cell_size():
    """CSI 16 t: cell size in device pixels -> (cell_w, cell_h) or None."""
    try:
        r = _query_ansi("\x1b[16t", b"t")
    except Exception:
        return None
    return parse_cell_size_reply(r)


def main(argv):
    url = "https://example.com"
    renderer = "auto"
    fps = 15
    scale = 1.0
    gpu = False
    self_test = False
    profile = None
    args = argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--self-test":
            self_test = True
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                url = args[i + 1]
                i += 1
        elif a == "--renderer" and i + 1 < len(args):
            renderer = args[i + 1]
            i += 1
        elif a == "--fps" and i + 1 < len(args):
            fps = max(1, min(60, int(args[i + 1])))
            i += 1
        elif a == "--profile" and i + 1 < len(args):
            profile = args[i + 1]
            i += 1
        elif a == "--scale" and i + 1 < len(args):
            scale = max(0.25, min(1.0, float(args[i + 1])))
            i += 1
        elif a == "--gpu":
            gpu = True
        elif not a.startswith("-"):
            url = a
        i += 1

    # terminal geometry first: sixel needs the pane's device-pixel size
    cols, rows = 120, 34
    try:
        c2, r2 = terminal_size()
        if c2 > 10 and r2 > 4:
            cols, rows = c2, r2
    except Exception:
        pass

    # renderer pick (queries need a tty; self-test needs none). Native
    # renderers do not require numpy/Pillow in the Python process.
    mode = "half"
    cell = (10, 20)
    if not self_test:
        try:
            kitty = False
            if os.name != "nt" and renderer in ("auto", "kitty"):
                kitty = query_kitty_support()
            sixel = False
            if renderer in ("auto", "sixel"):
                sixel = query_sixel_support()
                if sixel:
                    cs = query_cell_size()
                    if cs:
                        cell = cs
            if renderer == "kitty":
                kitty, sixel = True, False
            if renderer == "sixel":
                sixel, kitty = True, False
            if renderer == "half":
                kitty = sixel = False
            if kitty:
                mode = "kitty"
            elif sixel:
                mode = "sixel"
        except Exception:
            mode = "half"

    # engine viewport: sixel gets pane device pixels so the image fits 1:1.
    # --scale shrinks the engine viewport (below 1.0) to trade sharpness
    # for fps when the software raster is the bottleneck.
    if mode in ("sixel", "kitty"):
        eng_w = max(320, min(2400, int(cols * cell[0] * scale)))
        eng_h = max(200, min(1600, int(max(1, rows - 2) * cell[1] * scale)))
        if mode == "sixel":
            # Patches must align to both the 6px Sixel band grid and the
            # terminal's cell grid (lcm). An off-grid viewport bottom makes
            # the last band round up, overflow, and scroll the screen.
            import math
            super_h = 6 * cell[1] // math.gcd(6, cell[1])
            eng_h -= eng_h % super_h
    else:
        eng_w, eng_h = 1200, 760
    # geometry log: the numbers that decide readability; append for diagnosis
    try:
        with open(os.path.join(os.environ.get("TEMP", "."),
                               "browserr-term-geometry.log"), "a") as gf:
            gf.write(f"mode={mode} cols={cols} rows={rows} cell={cell} "
                     f"scale={scale} engine={eng_w}x{eng_h}\n")
    except OSError:
        pass

    log = os.path.join(os.environ.get("TEMP", "."), "browserr-term-engine.log")
    engine_args = ["--gpu"] if gpu else None
    if profile:
        d = Driver(profile_dir=profile, log=log, width=eng_w, height=eng_h,
                   engine_args=engine_args)
    else:
        d = Driver(ephemeral=True, log=log, width=eng_w, height=eng_h,
                   engine_args=engine_args)

    try:
        if self_test:
            return self_test_run(d, url, cols, rows)

        t = Term(d, cols, rows)
        kout = sys.stdout.buffer if hasattr(sys.stdout, "buffer") else sys.stdout
        if mode == "kitty":
            t.kitty = True
            t.cell = cell
            t.renderer = NativeKitty(kout, d, cols, max(1, rows - 2))
        elif mode == "sixel":
            t.sixel = True
            t.cell = cell
            t.renderer = NativeSixel(kout, cell)
        else:
            t.renderer = HalfblockDirty(cols, max(1, rows - 2), t.out)

        print(f"Browserr Term: {mode}, engine {eng_w}x{eng_h}, "
              f"terminal {cols}x{rows}, cell {cell[0]}x{cell[1]}, "
              f"max {fps}fps", file=sys.stderr)
        time.sleep(2)
        setup_terminal()
        if mode in ("sixel", "kitty"):
            sys.stdout.write(f"{ESC}[1;{t.view_rows}r{ESC}[1;1H")
            sys.stdout.flush()
        frame_min = 1.0 / fps
        last_render = 0.0
        last_meta = 0.0
        fps_t0 = time.time()
        try:
            t.go(url)
            rest = b""
            with RawInput() as inp:
                while t.running:
                    buf = inp.read(timeout=0.005)
                    if buf:
                        events, rest = parse_keys(rest + buf)
                        for ev in events:
                            if ev["kind"] == "mouse":
                                t.handle_mouse(ev)
                            else:
                                t.handle_key(ev)
                    now = time.time()
                    # Metadata: consume engine events (no eval round trips).
                    if now - last_meta > 0.5:
                        last_meta = now
                        t.poll_meta()
                        t.status = "ok" if t.title else "loading"
                    if now - last_render >= frame_min:
                        last_render = now
                        frame_t0 = time.perf_counter()
                        try:
                            if mode in ("sixel", "kitty"):
                                patches = d.native_frame(
                                    format=mode, tab=t.active,
                                    stream=t.active,
                                    cell_w=cell[0], cell_h=cell[1],
                                    force=t.force_next, timeout=5)
                                t.force_next = False
                                w, h, bgra = 0, 0, patches
                            else:
                                w, h, bgra = d.screenshot(tab=t.active)
                            t.last_frame_ms = (time.perf_counter() - frame_t0) * 1000
                            t.render_frame(w, h, bgra)
                            if now - fps_t0 > 1.0:
                                t.fps_now = t.frames / (now - fps_t0)
                                t.frames = 0
                                fps_t0 = now
                        except Exception as e:
                            t.status = f"err: {e}"
                            t.draw_status()
        finally:
            sys.stdout.write(f"{ESC}[r{RESET}")
            teardown_terminal()
        return 0
    finally:
        try:
            d.close()
        except Exception:
            pass


def self_test_run(d, url, cols, rows):
    """Headless protocol smoke: full frame, static skip, small patches."""
    d.navigate(url)
    d.wait_load()
    d.wait_ready()
    time.sleep(1.0)
    for fmt in ("png", "sixel"):
        full = d.native_frame(format=fmt, force=True)
        assert full and full[0][1] == (0, 0, d.width, d.height)
        assert len(full[0][4]) > 1000
        assert d.native_frame(format=fmt) == []
        print(f"native {fmt}: {len(full[0][4])} bytes, static skip OK")
    d.eval_js(
        "(function(){var e=document.createElement('div');"
        "e.style.cssText='position:fixed;left:8px;top:8px;width:100px;"
        "height:30px;background:#f0a';e.textContent='PATCH';"
        "document.body.appendChild(e)})()")
    time.sleep(0.6)
    patches = d.native_frame(format="sixel")
    assert patches and all(p[1][2] < d.width for p in patches)
    print(f"native Sixel patch: {len(patches)} rectangles, "
          f"{sum(len(p[4]) for p in patches)} bytes")
    print("SELF-TEST OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
