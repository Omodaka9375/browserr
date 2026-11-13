"""Browserr automation driver (like CDP for chrome.exe, but our own protocol).

Two modes sharing one API:

Headless (spawn a bare engine):
    from driver import Driver
    d = Driver()
    d.navigate("https://example.com"); d.wait_load(); d.wait_ready()
    print(d.eval_js("document.title"))

Computer use (attach to a running engine's driver port):
    engine.exe --ephemeral --driver-port=7412 --driver-token=secret
    from driver import Driver
    d = Driver.attach(7412, token="secret")
    d.navigate("https://example.com"); d.wait_ready()
    w, h, bgra = d.screenshot()

Capabilities: navigate, wait_load/wait_ready, eval_js (results returned as
JSON values), screenshot (BGRA buffer, save_png helper), click, move, wheel,
key, type_text, new_tab, activate, console capture, download, and
selector-based element commands (click_element, set_value,
wait_for_selector).

The engine's main port accepts exactly one client (the automation host);
the driver port accepts multiple drivers and mirrors engine
events/screenshots to all of them. With --driver-token, drivers must send
{"cmd":"auth","token":...} as their first line.
"""

import os as _os
TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
ROOT = _os.path.dirname(TOOLS_DIR)
# Engine binary name is platform-dependent (engine.exe on Windows, engine
# elsewhere); tests and the default engine path derive from this.
EXE_SUFFIX = ".exe" if _os.name == "nt" else ""
# Release layout: the engine binary ships next to driver.py (flat unpack).
# Prefer it when present so a downloaded archive works with no configuration;
# fall back to the source-tree build output for development checkouts.
_ENGINE_BUNDLED = TOOLS_DIR + "/engine" + EXE_SUFFIX
ENGINE_DEBUG = (_ENGINE_BUNDLED if _os.path.exists(_ENGINE_BUNDLED)
                else ROOT + "/engine/target/debug/engine" + EXE_SUFFIX)
ENGINE_RELEASE = (_ENGINE_BUNDLED if _os.path.exists(_ENGINE_BUNDLED)
                  else ROOT + "/engine/target/release/engine" + EXE_SUFFIX)
import json
import re
import socket
import struct
import subprocess
import threading
import time
import zlib
import os

FRM1 = 0x46524D31
EVT1 = 0x45565431
SHT1 = 0x53485431


class EngineDeadError(ConnectionError):
    """The engine process/socket died mid-session (crash, hang, kill).

    Raised by wait_for instead of hanging until the timeout; agents catch it
    (or let it bubble) and retry with a fresh Driver.
    """


class PausedError(RuntimeError):
    """The driver is paused: a human has taken over.

    Raised by every mutating action (navigate, input, key, wheel and the
    eval-based writers) so an agent loop fails loudly instead of acting
    while the user is in control. Reads keep working: the agent can still
    watch the page and wait for browser_resume.
    """


class StaleRefError(RuntimeError):
    """A stable ref ('e7') no longer resolves on the page.

    The page navigated (or the element went away) since the last
    read_structure(); refs reset on every read. Raised by click_ref
    instead of a silent None, so an agent loop fails loudly and
    re-reads the structure instead of believing it clicked.
    """


class NeedsConfirmError(RuntimeError):
    """The action is destructive and the page shows payment/captcha UI.

    Fail-closed gate for submit_form(): when detect_handoff() sees payment
    or captcha conditions, the submit refuses to run until the caller
    passes confirm=True (i.e. a human said yes in the loop). Never guessed.
    """


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


class Driver:
    """Drives Browserr over its automation protocol.

    Two modes:
      - Headless (default): spawns engine.exe and talks to it directly.
      - Computer use: Driver.attach(port=N) connects to a running
        engine started with --driver-port=N instead - same API.
    """

    @classmethod
    def attach(cls, port=7412, log=None, token=None):
        """Attach to a running engine started with --driver-port=N.
        If the engine was started with --driver-token=T, pass token=T: it is
        sent as the auth line before anything else."""
        self = cls.__new__(cls)
        self._init_common(port, log=log)
        last = None
        for _ in range(50):
            try:
                self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=1)
                break
            except OSError as e:
                last = e
                time.sleep(0.1)
        if self.sock is None:
            raise ConnectionError(f"no driver port on {self.port} (is the "
                                  f"browser running with --driver-port={port}?): {last}")
        if token:
            self.sock.sendall((json.dumps(
                {"cmd": "auth", "token": token}) + "\n").encode())
        threading.Thread(target=self._read_loop, daemon=True).start()
        return self

    def __init__(self, engine_path=None, profile_dir=None, port=7411,
                 width=1200, height=760, log=None, ephemeral=False,
                 engine_args=None):
        self._init_common(port, log=log)
        self.width = width
        self.height = height
        self.ephemeral = ephemeral

        engine_path = engine_path or os.environ.get("BROWSERR_ENGINE", ENGINE_DEBUG)
        args = [engine_path]
        if engine_args:
            # Extra engine CLI flags (e.g. --gpu) BEFORE the standard
            # profile flags; kept verbatim, no validation.
            args.extend(engine_args)
        if ephemeral:
            # Throwaway profile under %TEMP%; the engine deletes it on exit.
            args.append("--ephemeral")
        elif profile_dir:
            args.append(f"--profile-dir={profile_dir}")
        # stdout MUST be devnull: in an MCP server the engine would inherit
        # the JSON-RPC stdout pipe and corrupt it (chrome does the same:
        # Stdio::null()). The engine never legitimately writes to stdout.
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=self._log or subprocess.DEVNULL)

        last = None
        deadline = time.time() + 20.0
        while time.time() < deadline:
            try:
                self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=1)
                # Prove this socket belongs to a LIVE engine before using it:
                # a just-spawned engine can connect against a DYING engine's
                # listen backlog (both hold the port briefly during handover).
                # Ping/pong resolves it; a stale socket dies on the exchange.
                # The pong wait must be patient: the engine accepts BEFORE it
                # finishes initializing servo, and a cold start (llvmpipe GL on
                # a CI runner, cold caches) can take several seconds to reach
                # the command loop. A short timeout here closes the socket,
                # which kills the engine (main-client disconnect) - seen as a
                # first-test flake on Linux CI.
                req = self._next_id
                self._next_id += 1
                self.sock.sendall((json.dumps(
                    {"cmd": "ping", "tab": 0, "id": req}) + "\n").encode())
                self.sock.settimeout(8)
                pong = self._read_frame_raw()
                if (pong and pong.get("ev") == "pong"
                        and pong.get("id") == req):
                    break
                # Stale socket (old engine): close, retry on the new one.
                self.sock.close()
                self.sock = None
                time.sleep(0.1)
            except OSError as e:
                last = e
                self.sock = None
                time.sleep(0.1)
        if self.sock is None:
            if self.proc:
                self.proc.kill()
            detail = ""
            if self._log:
                self._log.flush()
                try:
                    with open(self._log.name, encoding="utf-8", errors="replace") as f:
                        detail = f.read()[-4000:].strip()
                except OSError:
                    pass
            raise ConnectionError(
                f"engine did not answer on {self.port}: {last}"
                + (f"; engine log: {detail}" if detail else ""))

        self.cmd({"cmd": "resize", "w": width, "h": height})
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_frame_raw(self):
        """Read one EVT1 frame off the socket without the reader thread
        (startup verification only). Returns the parsed JSON or None.
        Skips FRM1 frames (pixel data) if any arrive."""
        while True:
            hdr = b""
            while len(hdr) < 20:
                chunk = self.sock.recv(20 - len(hdr))
                if not chunk:
                    return None
                hdr += chunk
            magic = struct.unpack("<I", hdr[:4])[0]
            a = struct.unpack("<I", hdr[12:16])[0]
            b = struct.unpack("<I", hdr[16:20])[0]
            if magic == FRM1:
                # Skip pixel data, keep reading for the event.
                n = a * b * 4
                while n > 0:
                    chunk = self.sock.recv(min(n, 65536))
                    if not chunk:
                        return None
                    n -= len(chunk)
                continue
            if magic != EVT1:
                return None  # SHT1 or garbage: not what we're waiting for
            payload = b""
            while len(payload) < a:
                chunk = self.sock.recv(a - len(payload))
                if not chunk:
                    return None
                payload += chunk
            return json.loads(payload)

    def _init_common(self, port, log=None):
        """Shared state for both modes (headless spawn vs attach)."""
        self.port = port
        self.events = []            # all EVT1 events, newest last
        self.shots = []             # (w, h, bgra) screenshots, newest last
        self._next_id = 1
        self._event_seq = 0         # driver-local ordering for event cursors
        self._answered_requests = set()
        self._answered_auth = set()
        self._answered_filepickers = set()
        self._cond = threading.Condition()
        self._stop = False
        self.proc = None
        self.sock = None
        self._log = open(log, "w") if log else None
        self.width = None
        self.height = None
        self.dead = False           # set by the reader thread on socket loss
        self._paused = None        # reason string while a human has control
        self._handoff_seen = False

        # Trust layer state: evidence dir, action log, stored credentials.
        self.evidence_dir = None
        self.action_log_path = None
        self._credentials = {}     # host[:port] -> (user, password)
        self._auth_seen = set()    # auth event ids already answered
        self._auth_thread = None
        self._log_lock = threading.Lock()

    # -- protocol ---------------------------------------------------------

    # Engine commands that mutate the page. Everything else (ping, resize,
    # screenshot, eval reads) stays available while paused so the agent can
    # keep watching.
    _MUTATING_CMDS = frozenset((
        "navigate", "input", "key", "wheel", "new_tab", "activate",
        "close_tab", "back", "forward"))

    def _guard_paused(self, what: str) -> None:
        if self._paused is not None:
            raise PausedError(
                f"paused ({self._paused}): {what} blocked until resume()")

    def set_engine_paused(self, paused: bool) -> None:
        """Pause all automation clients while preserving native window input."""
        self.cmd({"cmd": "resume" if not paused else "pause", "tab": 0})

    def handoff_complete(self, timeout=2.0) -> bool:
        """Whether the human pressed Enter to finish this engine pause."""
        req = self._next_id
        self._next_id += 1
        self.cmd({"cmd": "handoff", "tab": 0, "id": req})
        ev = self.wait_for(lambda e: e.get("ev") == "handoff_status"
                           and e.get("id") == req, timeout)
        return bool(ev.get("complete"))

    def pause(self, reason="user takeover") -> None:
        """Hand control to the user. Every mutating action raises PausedError
        until resume(); reads (read_structure, read_page, screenshots,
        read_outcome) keep working. Driver-local: in attach mode other
        clients of the same engine are not affected."""
        self._paused = reason

    def resume(self) -> None:
        """Take control back after a user handoff."""
        self._paused = None

    @property
    def paused(self):
        """The pause reason, or None when the agent has control."""
        return self._paused

    def cmd(self, obj: dict) -> None:
        if self.dead:
            raise EngineDeadError("engine socket gone (cmd dropped)")
        if obj.get("cmd") in self._MUTATING_CMDS:
            self._guard_paused(obj["cmd"])
        try:
            self.sock.sendall((json.dumps(obj) + "\n").encode())
        except OSError as e:
            self._mark_dead()
            raise EngineDeadError(f"send failed: {e}")

    def _read_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("engine closed the connection")
            buf += chunk
        return buf

    def _read_loop(self):
        while not self._stop:
            try:
                self.sock.settimeout(0.5)
                hdr = self._read_exact(20)
            except socket.timeout:
                continue
            except (OSError, ConnectionError):
                self._mark_dead()
                return
            magic = struct.unpack("<I", hdr[:4])[0]
            a = struct.unpack("<I", hdr[12:16])[0]
            b = struct.unpack("<I", hdr[16:20])[0]
            try:
                if magic == FRM1:            # live frame: skip
                    self._read_exact(a * b * 4)
                elif magic == SHT1:          # screenshot frame
                    data = self._read_exact(a * b * 4)
                    with self._cond:
                        self.shots.append((a, b, data))
                        self._cond.notify_all()
                elif magic == EVT1:
                    payload = self._read_exact(a)
                    with self._cond:
                        event = json.loads(payload)
                        self._event_seq += 1
                        event["_driver_seq"] = self._event_seq
                        self.events.append(event)
                        self._cond.notify_all()
                else:
                    self._mark_dead()
                    return  # unknown stream: bail out
            except (OSError, ConnectionError):
                self._mark_dead()
                return

    def _mark_dead(self):
        """Record engine death and wake every waiter so they fail fast."""
        with self._cond:
            self.dead = True
            self._cond.notify_all()

    def event_cursor(self):
        """A cursor for waiting only on events received after this point."""
        with self._cond:
            return self._event_seq

    def wait_for(self, pred, timeout=10.0, after=None):
        """Wait until pred(event) holds for some event, return that event.
        after is an optional event_cursor(); older events are ignored.
        Raises EngineDeadError as soon as the reader notices the engine
        socket is gone (no hanging until the timeout)."""
        deadline = time.time() + timeout
        with self._cond:
            while True:
                for e in reversed(self.events):
                    if ((after is None or e.get("_driver_seq", 0) > after)
                            and pred(e)):
                        return e
                if self.dead:
                    raise EngineDeadError(
                        "engine socket gone; recent events: "
                        + str([e.get("ev") for e in self.events[-10:]]))
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError("no matching event; recent: "
                                       f"{[e.get('ev') for e in self.events[-10:]]}")
                self._cond.wait(min(remaining, 0.25))

    # -- high level -------------------------------------------------------

    def navigate(self, url: str, tab: int = 0) -> None:
        self._guard_paused("navigate")
        self._log_action("navigate", url=url)
        self.cmd({"cmd": "navigate", "url": url, "tab": tab})

    def wait_load(self, tab: int = 0, timeout=15.0, contains: str = None):
        """Wait for the tab's url event (optionally containing a substring)."""
        return self.wait_for(
            lambda e: e.get("ev") == "url" and e.get("tab") == tab
            and (contains is None or contains in e.get("url", "")),
            timeout)

    def wait_ready(self, tab: int = 0, timeout=30.0) -> None:
        """Wait until the page can run script and receive input.

        The url event fires when navigation commits, which can be well before
        the document accepts input (early clicks report dispatch_failed); poll
        document.readyState instead. Evals sent too early are dropped by the
        engine, so short-timeout retries are the mechanism.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.eval_js("document.readyState", tab, timeout=1.5) == "complete":
                    return
            except TimeoutError:
                pass
            time.sleep(0.2)
        raise TimeoutError("page never became ready")

    def eval_js(self, js: str, tab: int = 0, timeout=10.0):
        """Evaluate JS in the page and return the (JSON-friendly) result."""
        req = self._next_id
        self._next_id += 1
        self.cmd({"cmd": "eval", "tab": tab, "js": js, "id": req})
        ev = self.wait_for(
            lambda e: e.get("ev") == "evalresult" and e.get("id") == req, timeout)
        return ev.get("value")

    def screenshot(self, tab: int = 0, timeout=10.0):
        """Paint the tab and return (w, h, bgra bytes)."""
        n = len(self.shots)
        self.cmd({"cmd": "screenshot", "tab": tab})
        deadline = time.time() + timeout
        with self._cond:
            while len(self.shots) <= n:
                if self.dead:
                    raise EngineDeadError("engine socket gone (no screenshot)")
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError("no screenshot arrived")
                self._cond.wait(min(remaining, 0.25))
        return self.shots[-1]

    def screenshot_annotated(self, path, tab: int = 0, timeout=15.0):
        """Screenshot with every interactive element boxed and numbered by
        its stable ref (element 7 is ref e7), plus the item list, so an
        agent and a human can both point at the same element.

        Returns {path, w, h, items: [{ref, num, label, x, y, w, h}],
        pixels_drawn}."""
        r = self.read_structure(tab, timeout)
        dpr = self.eval_js("window.devicePixelRatio || 1", tab, timeout) or 1
        w, h, bgra = self.screenshot(tab)
        buf = bytearray(bgra)
        items = []
        drawn = 0
        for group in ("buttons", "inputs", "selects"):
            for el in (r.get(group) or []):
                ref = str(el.get("ref") or "")
                if not ref[1:].isdigit():
                    continue
                num = int(ref[1:])
                bw, bh = int(el.get("w") or 0), int(el.get("h") or 0)
                if bw <= 0 or bh <= 0:
                    continue
                x = int((el.get("x") or 0) * dpr)
                y = int((el.get("y") or 0) * dpr)
                bw, bh = int(bw * dpr), int(bh * dpr)
                drawn += draw_rect(buf, w, h, x, y, bw, bh)
                drawn += draw_number(buf, w, h, x + 2, y + 2, num)
                items.append({"ref": ref, "num": num,
                              "label": el.get("label") or el.get("text") or "",
                              "x": x, "y": y, "w": bw, "h": bh})
        save_png(path, w, h, bytes(buf))
        return {"path": path, "w": w, "h": h, "items": items,
                "pixels_drawn": drawn}

    def click(self, x, y, tab: int = 0) -> None:
        self.cmd({"cmd": "input", "tab": tab, "kind": "down", "x": x, "y": y, "button": "left"})
        self.cmd({"cmd": "input", "tab": tab, "kind": "up", "x": x, "y": y, "button": "left"})

    def move(self, x, y, tab: int = 0) -> None:
        self.cmd({"cmd": "input", "tab": tab, "kind": "move", "x": x, "y": y, "button": "none"})

    def wheel(self, dy, x=10, y=10, tab: int = 0) -> None:
        self.cmd({"cmd": "wheel", "tab": tab, "dx": 0, "dy": dy, "x": x, "y": y})

    def key(self, name, text="", mods=0, down=True, tab: int = 0) -> None:
        self.cmd({"cmd": "key", "tab": tab, "name": name, "text": text,
                  "mods": mods, "down": down})

    def type_text(self, s: str, tab: int = 0) -> None:
        for ch in s:
            self.key("text", ch, down=True, tab=tab)
            self.key("text", ch, down=False, tab=tab)

    # -- element commands (selector-based; built on eval + real input) ------

    @staticmethod
    def _js_selector(selector: str) -> str:
        """Embed a CSS selector as a JS string literal (json quoting)."""
        return json.dumps(selector)

    def element_rect(self, selector, tab: int = 0, timeout=10.0):
        """Scroll the first element matching a CSS selector into view and
        return its viewport rect {x, y, w, h} (center = x+w/2, y+h/2),
        or None while it does not exist yet."""
        js = ("(function(){var s=" + self._js_selector(selector) + ";"
              "var e=document.querySelector(s);if(!e){return null;}"
              "e.scrollIntoView({block:'center',inline:'nearest'});"
              "var r=e.getBoundingClientRect();"
              "return {x:r.x,y:r.y,w:r.width,h:r.height};})()")
        deadline = time.time() + timeout
        while True:
            try:
                rect = self.eval_js(js, tab, timeout=2.0)
            except TimeoutError:
                rect = None  # page not scriptable yet; keep polling
            if rect:
                return rect
            if time.time() >= deadline:
                return None
            time.sleep(0.2)

    def click_element(self, selector, tab: int = 0, timeout=10.0):
        """Click the center of the first element matching a CSS selector with
        a real pointer event (down+up at coordinates, so hover/focus behave).
        Returns (x, y); raises TimeoutError if the element never appears."""
        deadline = time.time() + timeout
        while True:
            rect = self.element_rect(selector, tab, timeout=2.0)
            if rect:
                x = rect["x"] + rect["w"] / 2
                y = rect["y"] + rect["h"] / 2
                self.move(x, y, tab=tab)
                self.click(x, y, tab=tab)
                return (x, y)
            if time.time() >= deadline:
                raise TimeoutError(f"no element matches {selector!r}")
            time.sleep(0.2)

    def set_value(self, selector, value, tab: int = 0, timeout=10.0):
        """Set an input/textarea/select value by CSS selector, firing input
        and change events. Uses the native value setter so React/Vue
        controlled inputs accept the change. Blocked while paused.
        Returns True on success."""
        self._guard_paused("set_value")
        js = ("(function(){var s=" + self._js_selector(selector) + ";"
              "var v=" + json.dumps(value) + ";"
              "var e=document.querySelector(s);if(!e){return {ok:false};}"
              "e.focus();"
              "var proto=e.tagName==='TEXTAREA'?"
              "window.HTMLTextAreaElement.prototype:"
              "window.HTMLInputElement.prototype;"
              "var d=Object.getOwnPropertyDescriptor(proto,'value');"
              "if(d&&d.set){d.set.call(e,v);}else{e.value=v;}"
              "e.dispatchEvent(new Event('input',{bubbles:true}));"
              "e.dispatchEvent(new Event('change',{bubbles:true}));"
              "return {ok:true};})()")
        deadline = time.time() + timeout
        while True:
            try:
                r = self.eval_js(js, tab, timeout=2.0)
            except TimeoutError:
                r = None  # page not scriptable yet; keep polling
            if r and r.get("ok"):
                return True
            if time.time() >= deadline:
                raise TimeoutError(f"no input matches {selector!r}")
            time.sleep(0.2)

    def wait_for_selector(self, selector, tab: int = 0, timeout=15.0):
        """Wait until a CSS selector matches at least one visible element;
        returns the rect (see element_rect) or raises TimeoutError."""
        deadline = time.time() + timeout
        while True:
            rect = self.element_rect(selector, tab, timeout=2.0)
            if rect and rect["w"] > 0 and rect["h"] > 0:
                return rect
            if time.time() >= deadline:
                raise TimeoutError(f"no element matches {selector!r}")
            time.sleep(0.2)

    def new_tab(self, tab: int) -> None:
        self.cmd({"cmd": "new_tab", "tab": tab})

    def activate(self, tab: int) -> None:
        self.cmd({"cmd": "activate", "tab": tab})

    def ping(self, timeout=5.0):
        """Health check: returns {'tabs': N, 'active': A} or raises TimeoutError.
        Works in both modes (headless and attach)."""
        req = self._next_id
        self._next_id += 1
        self.cmd({"cmd": "ping", "tab": 0, "id": req})
        ev = self.wait_for(lambda e: e.get("ev") == "pong" and e.get("id") == req, timeout)
        return {"tabs": ev.get("tabs"), "active": ev.get("active")}

    def console(self, tab=None, level=None):
        """Collected page console messages as dicts
        {tab, level, message} (level: log/info/warn/error/...).
        Optional filters by tab and level."""
        return [e for e in self.events
                if e.get("ev") == "console"
                and (tab is None or e.get("tab") == tab)
                and (level is None or e.get("level") == level)]

    def wait_console(self, level=None, contains=None, tab=None, timeout=15.0):
        """Wait for a console message (optionally matching level and a
        substring); returns the message event."""
        return self.wait_for(
            lambda e: e.get("ev") == "console"
            and (level is None or e.get("level") == level)
            and (contains is None or contains in e.get("message", ""))
            and (tab is None or e.get("tab") == tab),
            timeout)

    # -- fail-closed interception / auth / file pickers ----------------------

    def intercept(self, on: bool) -> None:
        """Arm/disarm fail-closed request interception.

        When armed, qualifying requests (main-frame documents, fetch/XHR and
        JSON loads) are held for a decision; the engine emits
        {"ev":"request","id","tab","method","url","main"} events and the page
        load stalls until request_answer() (or the engine's 5s fail-closed
        timeout denies it).
        """
        self.cmd({"cmd": "intercept", "tab": 0, "on": bool(on)})

    def wait_request(self, tab=None, url_contains=None, timeout=10.0):
        """Wait for an unanswered held-request decision event; returns it."""
        return self.wait_for(
            lambda e: e.get("ev") == "request"
            and e.get("id") not in self._answered_requests
            and (tab is None or e.get("tab") == tab)
            and (url_contains is None or url_contains in e.get("url", "")),
            timeout)

    def request_answer(self, req_id, action: str, status=200, body="",
                       content_type="text/plain"):
        """Answer a held request: "allow" passes it through, "deny" fulfills
        it with a synthetic 403, "respond" fulfills it with status/body."""
        self.cmd({"cmd": "request", "tab": 0, "id": req_id, "action": action,
                  "status": status, "body": body, "content_type": content_type})
        with self._cond:
            self._answered_requests.add(req_id)

    def wait_auth(self, tab=None, timeout=15.0, after=None):
        """Wait for an unanswered auth challenge event
        {"ev":"auth","id","tab","url","proxy"}; returns it."""
        return self.wait_for(
            lambda e: e.get("ev") == "auth"
            and e.get("id") not in self._answered_auth
            and (tab is None or e.get("tab") == tab),
            timeout, after=after)

    def auth_answer(self, req_id, user=None, password=None):
        """Answer an auth challenge. Without credentials the challenge is
        failed closed (no credentials sent)."""
        args = {"cmd": "auth", "tab": 0, "id": req_id}
        if user is not None and password is not None:
            args.update({"user": user, "pass": password})
        self.cmd(args)
        with self._cond:
            self._answered_auth.add(req_id)

    def wait_filepicker(self, tab=None, timeout=15.0):
        """Wait for an unanswered file-picker event
        {"ev":"filepicker","id","tab","multiple"}; returns it."""
        return self.wait_for(
            lambda e: e.get("ev") == "filepicker"
            and e.get("id") not in self._answered_filepickers
            and (tab is None or e.get("tab") == tab),
            timeout)

    def files_answer(self, req_id, paths=None):
        """Answer a file picker with a list of paths (empty/None = cancel)."""
        self.cmd({"cmd": "files", "tab": 0, "id": req_id,
                 "paths": list(paths) if paths else []})
        with self._cond:
            self._answered_filepickers.add(req_id)

    # -- net evidence / frames ------------------------------------------------

    def net_evidence(self, limit=200, tab: int = 0, timeout=10.0):
        """Recent page network activity (fetch/XHR) from the ring buffer
        the document-start user script keeps on window.__browserr_net.
        Runs in every frame; this reads the top frame's ring."""
        js = ("JSON.stringify((window.__browserr_net||[]).slice(-%d))" % limit)
        raw = self.eval_js(js, tab, timeout)
        if isinstance(raw, str):
            return json.loads(raw)
        return raw or []

    def frames(self, tab: int = 0, timeout=10.0):
        """The page's frame tree: {url, origin, children:[...]}.
        Same-origin frames are walked (title, nested children); cross-origin
        frames report their url only (servo 0.8 has no cross-frame JS)."""
        req = self._next_id
        self._next_id += 1
        self.cmd({"cmd": "frames", "tab": tab, "id": req})
        ev = self.wait_for(
            lambda e: e.get("ev") == "evalresult" and e.get("id") == req, timeout)
        value = ev.get("value")
        if isinstance(value, str):
            return json.loads(value)
        return value

    def download(self, url, path, max_bytes=None, timeout=120.0):
        """Fetch a URL straight to a local path (agents fetch files; servo
        0.8 has no in-page download hook). No page cookies are sent, so only
        public URLs work. Returns the byte count."""
        req = self._next_id
        self._next_id += 1
        args = {"cmd": "download", "tab": 0, "id": req, "url": url, "path": path}
        if max_bytes:
            args["max_bytes"] = max_bytes
        self.cmd(args)
        ev = self.wait_for(
            lambda e: e.get("ev") == "download" and e.get("id") == req, timeout)
        if not ev.get("ok"):
            raise RuntimeError(f"download failed: {ev.get('error')}")
        return ev.get("bytes")

    def close(self, timeout=5.0) -> None:
        """Detach (attach mode leaves the browser running) or stop the engine.

        Headless mode: closing the socket is the engine's normal shutdown path
        (it exits when its single client disconnects, cleaning up any ephemeral
        profile); wait briefly for that before falling back to a kill.
        """
        self._stop = True
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        if self.proc:
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._log:
            self._log.close()

    # -- SDK helpers (page extraction for agents) ---------------------------

    READ_JS = r"""(() => {
        const pick = (el) => {
            // Skip invisible branches entirely.
            const st = getComputedStyle(el);
            if (st.display === 'none' || st.visibility === 'hidden') return [];
            const out = [];
            for (const n of el.childNodes) {
                if (n.nodeType === 3) {
                    const t = n.textContent.replace(/\s+/g, ' ').trim();
                    if (t) out.push(t);
                } else if (n.nodeType === 1) {
                    const tag = n.tagName;
                    if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT' || tag === 'TEMPLATE') continue;
                    const inner = pick(n);
                    if (tag === 'A' && inner.length) {
                        out.push('[' + inner.join(' ') + '](' + n.href + ')');
                    } else if ((tag === 'P' || tag === 'LI' || tag === 'H1' || tag === 'H2' || tag === 'H3' || tag === 'H4' || tag === 'BLOCKQUOTE') && inner.length) {
                        out.push('\n\n' + inner.join(' '));
                    } else {
                        out.push(...inner);
                    }
                }
            }
            return out;
        };
        return [document.title, pick(document.body).join(' ')];
    })()"""

    LINKS_JS = r"""(() => {
        const out = [];
        for (const a of document.links) {
            const r = a.getBoundingClientRect();
            if (!r.width || !r.height) continue;
            const text = a.innerText.replace(/\s+/g, ' ').trim() || a.href;
            out.push({text: text, href: a.href, x: r.x, y: r.y, w: r.width, h: r.height});
        }
        return out;
    })()"""

    FORMS_JS = r"""(() => {
        const visible = (el) => {
            const st = getComputedStyle(el);
            return st.display !== 'none' && st.visibility !== 'hidden';
        };
        const fields = (form) => {
            const out = [];
            for (const i of form.querySelectorAll('input, textarea, select')) {
                if (!visible(i)) continue;
                const r = i.getBoundingClientRect();
                out.push({
                    tag: i.tagName.toLowerCase(), type: i.type || '',
                    name: i.name || '', id: i.id || '',
                    placeholder: i.placeholder || '',
                    value: i.value || '',
                    x: r.x, y: r.y, w: r.width, h: r.height,
                });
            }
            return out;
        };
        const out = [];
        for (const f of document.forms) {
            const fs = fields(f);
            if (!fs.length) continue;
            out.push({action: f.action || location.href, method: f.method, fields: fs});
        }
        // Password fields outside any <form> (common in SPA logins).
        for (const p of document.querySelectorAll('input[type=password]')) {
            if (!p.closest('form') && visible(p)) {
                const r = p.getBoundingClientRect();
                out.push({action: location.href, method: 'none', fields: [{
                    tag: 'input', type: 'password', name: p.name || '', id: p.id || '',
                    placeholder: p.placeholder || '', value: '',
                    x: r.x, y: r.y, w: r.width, h: r.height,
                }]});
            }
        }
        return out;
    })()"""

    STRUCTURE_JS = r"""(() => {
        // Structured page inventory: interactive elements + headings, with
        // rects, a usable CSS selector and a STABLE REF (e1, e2, ...) that
        // an agent and a human can both point at. Form fields also carry
        // their label, constraints, current state and computed validity.
        // Shadow DOM is pierced (querySelector does not cross shadowRoot).
        const sel = (el) => {
            if (el.id) return '#' + CSS.escape(el.id);
            const name = el.getAttribute('name');
            if (name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(name)}"]`;
            // Nth-of-type path from <body>; unique in practice for the
            // pages agents drive.
            const parts = [];
            let n = el;
            while (n && n.tagName !== 'BODY' && n.parentElement) {
                const sibs = [...n.parentElement.children].filter(
                    c2 => c2.tagName === n.tagName);
                const i = sibs.indexOf(n) + 1;
                parts.unshift(n.tagName.toLowerCase()
                    + (sibs.length > 1 ? `:nth-of-type(${i})` : ''));
                n = n.parentElement;
            }
            return parts.join(' > ');
        };
        const rect = (el) => {
            const r = el.getBoundingClientRect();
            return {x: r.x, y: r.y, w: r.width, h: r.height};
        };
        const txt = (n, max) => ((n && n.textContent) || '')
            .replace(/\s+/g, ' ').trim().slice(0, max);
        const vis = (el) => {
            const s = getComputedStyle(el);
            if (s.display === 'none' || s.visibility === 'hidden') return false;
            if (parseFloat(s.opacity) === 0) return false;
            return true;
        };

        // Label resolution: aria-labelledby, aria-label, <label for>,
        // wrapping <label>, then placeholder/title as a last resort.
        const labelOf = (el) => {
            const ids = el.getAttribute('aria-labelledby');
            if (ids) {
                const t = ids.split(/\s+/).map(id => document.getElementById(id))
                    .filter(Boolean).map(n => txt(n, 120)).join(' ').trim();
                if (t) return t;
            }
            const al = el.getAttribute('aria-label');
            if (al && al.trim()) return al.trim().slice(0, 120);
            if (el.id) {
                const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                if (l) { const t = txt(l, 120); if (t) return t; }
            }
            const wrap = el.closest ? el.closest('label') : null;
            if (wrap) { const t = txt(wrap, 120); if (t) return t; }
            if (el.tagName === 'INPUT'
                && (el.type === 'submit' || el.type === 'button')) return el.value || '';
            return (el.getAttribute('placeholder') || el.getAttribute('title') || '').slice(0, 120);
        };

        // Constraints read from attributes: servo 0.8's constraint API is
        // partial, so validity is computed here instead.
        const constraintsOf = (el) => {
            const c = {};
            if (el.hasAttribute('required')
                || el.getAttribute('aria-required') === 'true') c.required = true;
            for (const a of ['pattern', 'minlength', 'maxlength', 'min', 'max',
                             'step', 'autocomplete', 'inputmode']) {
                const v = el.getAttribute(a);
                if (v !== null && v !== '') c[a] = v;
            }
            return c;
        };
        const validityOf = (el, c) => {
            const reasons = [];
            const type = (el.type || '').toLowerCase();
            const val = String(el.value == null ? '' : el.value);
            if (!el.disabled && !el.readOnly) {
                if (c.required) {
                    const empty = (type === 'checkbox' || type === 'radio')
                        ? !el.checked : val.trim() === '';
                    if (empty) reasons.push('required');
                }
                if (c.pattern && val) {
                    try {
                        if (!new RegExp('^(?:' + c.pattern + ')$').test(val)) reasons.push('pattern');
                    } catch (_) {}
                }
                if (c.maxlength && val.length > +c.maxlength) reasons.push('maxlength');
                if (c.minlength && val && val.length < +c.minlength) reasons.push('minlength');
                if (type === 'email' && val
                    && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(val)) reasons.push('email');
                if (type === 'url' && val) {
                    try { new URL(val); } catch (_) { reasons.push('url'); }
                }
                if ((type === 'number' || type === 'range') && val) {
                    const n = parseFloat(val);
                    if (!isNaN(n)) {
                        if (c.min !== undefined && n < parseFloat(c.min)) reasons.push('min');
                        if (c.max !== undefined && n > parseFloat(c.max)) reasons.push('max');
                    }
                }
            }
            return reasons;
        };

        // One deep pass: every element, shadow roots included (depth-capped).
        const all = [];
        const walk = (root, depth) => {
            if (depth > 8) return;
            for (const el of root.querySelectorAll('*')) {
                all.push(el);
                if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
            }
        };
        walk(document, 0);

        // Fresh refs each read: a ref from an older page resolves to
        // nothing, so callers get a clean 'not found'.
        const refs = {};
        window.__browserr_refs = refs;
        let seq = 0;
        const ref = (el) => {
            seq += 1;
            const r = 'e' + seq;
            refs[r] = el;
            return r;
        };

        const out = {title: document.title, url: location.href,
                     headings: [], buttons: [], inputs: [], selects: [],
                     links: [], images: [], forms: []};
        const formIndex = new Map();
        const formOf = (f) => {
            if (!formIndex.has(f)) {
                formIndex.set(f, out.forms.length);
                out.forms.push({id: f.id || '', name: f.name || '',
                                action: f.action || '',
                                method: (f.method || 'get').toLowerCase(),
                                enctype: f.enctype || '', fields: [], submits: []});
            }
            return out.forms[formIndex.get(f)];
        };

        const field = (el) => {
            const tag = el.tagName.toLowerCase();
            let type = (el.type || 'text').toLowerCase();
            if (tag === 'textarea') type = 'textarea';
            if (el.isContentEditable && tag !== 'textarea' && tag !== 'input') type = 'richtext';
            const c = constraintsOf(el);
            const reasons = validityOf(el, c);
            const o = {
                ref: ref(el), tag: tag, type: type,
                name: el.name || '', id: el.id || '',
                label: labelOf(el),
                placeholder: el.placeholder || '',
                selector: sel(el), ...rect(el),
                disabled: !!el.disabled, readonly: !!el.readOnly,
                focused: document.activeElement === el,
            };
            if (type === 'checkbox' || type === 'radio') {
                o.checked = !!el.checked;
                if (type === 'radio' && el.name) o.group = el.name;
            } else if (el.isContentEditable) {
                o.value = txt(el, 200);
            } else {
                o.value = String(el.value == null ? '' : el.value).slice(0, 200);
            }
            if (Object.keys(c).length) o.constraints = c;
            o.valid = reasons.length === 0;
            if (reasons.length) o.invalid = reasons;
            const f = el.form;
            if (f) {
                const meta = formOf(f);
                meta.fields.push(o.ref);
                o.form = {id: meta.id, action: meta.action, method: meta.method};
            }
            return o;
        };

        for (const el of all) {
            if (/^H[1-6]$/.test(el.tagName) && vis(el)) {
                out.headings.push({level: +el.tagName[1], text: txt(el, 200)});
            }
        }
        for (const el of all) {
            const tag = el.tagName;
            const isBtn = tag === 'BUTTON'
                || el.getAttribute('role') === 'button'
                || (tag === 'INPUT'
                    && ['submit', 'button', 'reset', 'image'].includes((el.type || '').toLowerCase()));
            if (!isBtn || !vis(el)) continue;
            const r = rect(el);
            if (!r.w && !r.h) continue;
            const o = {ref: ref(el), text: txt(el, 80) || (el.value || ''),
                       type: (el.type || 'button').toLowerCase(),
                       selector: sel(el), ...r, disabled: !!el.disabled};
            out.buttons.push(o);
            const f = el.form;
            if (f) formOf(f).submits.push(o.ref);
        }
        for (const el of all) {
            const tag = el.tagName;
            const type = (el.type || '').toLowerCase();
            const skip = tag === 'INPUT'
                && ['submit', 'button', 'reset', 'image', 'hidden'].includes(type);
            const isField = (tag === 'INPUT' && !skip) || tag === 'TEXTAREA'
                || (el.isContentEditable && tag !== 'textarea' && tag !== 'input');
            if (!isField || !vis(el)) continue;
            const r = rect(el);
            if (!r.w && !r.h) continue;
            out.inputs.push(field(el));
        }
        for (const el of all) {
            if (el.tagName !== 'SELECT' || !vis(el)) continue;
            const o = field(el);
            o.multiple = !!el.multiple;
            o.options = [...el.options].slice(0, 80).map(x => ({
                value: x.value, label: txt(x, 60),
                disabled: !!x.disabled, selected: !!x.selected}));
            out.selects.push(o);
        }
        for (const a of document.links) {
            const r = a.getBoundingClientRect();
            if (!r.width || !r.height) continue;
            const text = a.innerText.replace(/\s+/g, ' ').trim() || a.href;
            out.links.push({text: text.slice(0, 80), href: a.href,
                            x: r.x, y: r.y, w: r.width, h: r.height});
        }
        for (const im of document.images) {
            const r = im.getBoundingClientRect();
            if (!r.width || !r.height) continue;
            out.images.push({alt: (im.alt || '').slice(0, 80), src: im.src.slice(0, 200),
                             x: r.x, y: r.y, w: r.width, h: r.height});
        }
        return out;
    })()"""

    REF_RECT_JS = r"""(() => {
        // Resolve a stable ref from the last read_structure() to a click
        // point. Stale refs (page navigated since) resolve to null.
        const el = (window.__browserr_refs || {})[%s];
        if (!el || !el.isConnected) return null;
        el.scrollIntoView({block: 'center', inline: 'center'});
        const r = el.getBoundingClientRect();
        return {x: r.x + r.width / 2, y: r.y + r.height / 2,
                w: r.width, h: r.height};
    })()"""

    SET_FIELD_JS = r"""(() => {
        // Type-aware field write: checkbox/radio toggle, <select> option by
        // value or label, contenteditable text, plain value otherwise. All
        // paths fire input + change + blur so frameworks and validators run.
        const key = %s, value = %s;
        const el = /^e\d+$/.test(key)
            ? (window.__browserr_refs || {})[key]
            : document.querySelector(key);
        if (!el || !el.isConnected) return {ok: false, error: 'not found: ' + key};
        if (el.focus) { try { el.focus(); } catch (_) {} }
        const tag = el.tagName.toLowerCase();
        const type = (el.type || '').toLowerCase();
        let how = 'value';
        if (type === 'checkbox' || type === 'radio') {
            const want = value === true || value === 1
                || ['true', '1', 'checked', 'yes', 'on'].includes(String(value).toLowerCase());
            if (!!el.checked !== want) {
                // Native click fires input and change already.
                el.click();
                el.dispatchEvent(new Event('blur', {bubbles: true}));
                return {ok: true, how: 'checked', value: !!el.checked};
            }
            how = 'checked';
        } else if (tag === 'select') {
            const opts = [...el.options];
            const v = String(value).trim();
            const hit = opts.find(o => o.value === v)
                || opts.find(o => (o.text || '').trim().toLowerCase() === v.toLowerCase());
            if (!hit) return {ok: false, error: 'no option: ' + v,
                              options: opts.map(o => o.value).slice(0, 60)};
            el.value = hit.value;
            how = 'option';
        } else if (el.isContentEditable) {
            el.textContent = String(value);
            how = 'richtext';
        } else {
            el.value = String(value);
        }
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        el.dispatchEvent(new Event('blur', {bubbles: true}));
        return {ok: true, how: how,
                value: (type === 'checkbox' || type === 'radio') ? !!el.checked : el.value};
    })()"""

    def read_page(self, tab: int = 0, timeout=15.0):
        """Extract the page as (title, markdown-ish text): links become
        [label](href), paragraphs get blank-line separation. Token-cheap."""
        title, text = self.eval_js(self.READ_JS, tab, timeout)
        return title, text

    def read_structure(self, tab: int = 0, timeout=15.0):
        """Structured page inventory: headings, buttons, inputs, selects,
        links and images, each with a viewport rect, a usable CSS selector
        and a stable ref (e1, e2, ...). Form fields carry their label,
        constraints, current state and computed validity; forms group their
        fields and submit controls. Shadow DOM is pierced. Richer than
        read_page; use when the agent needs to locate and act on specific UI."""
        r = self.eval_js(self.STRUCTURE_JS, tab, timeout)
        if not r or not isinstance(r, dict):
            return {"title": "", "headings": [], "buttons": [], "inputs": [],
                    "selects": [], "links": [], "images": []}
        for key in ("headings", "buttons", "inputs", "selects", "links", "images"):
            r.setdefault(key, [])
        return r

    def links(self, tab: int = 0, timeout=10.0):
        """Visible links with their viewport rectangles (for click targeting)."""
        return self.eval_js(self.LINKS_JS, tab, timeout) or []

    def forms(self, tab: int = 0, timeout=10.0):
        """Visible forms and their fields, with viewport rectangles."""
        return self.eval_js(self.FORMS_JS, tab, timeout) or []

    def click_text(self, text, tab: int = 0, timeout=5.0):
        """Click the first visible link whose label contains `text`.
        Returns the link's (x, y) center, or None if no match."""
        text_l = text.lower()
        for l in self.links(tab, timeout):
            if text_l in (l.get("text") or "").lower():
                x = l["x"] + l["w"] / 2
                y = l["y"] + l["h"] / 2
                self.click(x, y, tab)
                return x, y
        return None

    def fill(self, name_or_id, value, tab: int = 0, timeout=10.0):
        """Set a form field by name/id and fire input+change (framework-safe).
        Blocked while paused. Returns True if a field was found and filled."""
        self._guard_paused("fill")
        js = ("((()=>{const el=document.getElementsByName(%s)[0]"
              "||document.getElementById(%s);"
              "if(!el)return false;el.focus();el.value=%s;"
              "el.dispatchEvent(new Event('input',{bubbles:true}));"
              "el.dispatchEvent(new Event('change',{bubbles:true}));return true;})())"
              % (json.dumps(name_or_id), json.dumps(name_or_id), json.dumps(value)))
        return bool(self.eval_js(js, tab, timeout))

    def click_ref(self, ref, tab: int = 0, timeout=10.0):
        """Click the center of a stable ref from the last read_structure().
        Scrolls it into view first. Returns the click point; raises
        StaleRefError when the ref no longer resolves (the page navigated
        since the read) instead of silently no-oping."""
        pt = self.eval_js(self.REF_RECT_JS % json.dumps(ref), tab, timeout)
        if not pt or not isinstance(pt, dict):
            raise StaleRefError(
                f"stale ref {ref!r}: run read_structure again for fresh refs")
        # hover first: servo resolves the click target from the pointer
        # position, so a move before down/up is what makes hover/focus behave.
        self.move(pt["x"], pt["y"], tab=tab)
        self.click(pt["x"], pt["y"], tab=tab)
        self._log_action("click_ref", ref=ref)
        return pt

    def set_field(self, ref_or_selector, value, tab: int = 0, timeout=10.0):
        """Type-aware field write: a stable ref ('e7') or a CSS selector.

        Writes are blocked while paused (a human has control).
        Checkbox/radio take a boolean ('checked'/'1'/true...), <select>
        takes an option value or its label, contenteditable takes text,
        everything else takes a plain value. Fires input, change and blur
        so framework bindings and validators run. Returns {ok, how, value}
        or {ok: False, error}."""
        self._guard_paused("set_field")
        js = self.SET_FIELD_JS % (json.dumps(ref_or_selector), json.dumps(value))
        return self.eval_js(js, tab, timeout)

    FILL_FORM_JS = r"""(() => {
        // Batch field write: ONE round trip for a whole form. `plan` maps a
        // stable ref ('e7') or a CSS selector to the value to write; the
        // write is type-aware, same rules as set_field.
        const plan = %s;
        const refs = window.__browserr_refs || {};
        const resolve = (key) => /^e\d+$/.test(key)
            ? refs[key] : document.querySelector(key);
        const apply = (el, value) => {
            if (el.focus) { try { el.focus(); } catch (_) {} }
            const tag = el.tagName.toLowerCase();
            const type = (el.type || '').toLowerCase();
            let how = 'value';
            if (type === 'checkbox' || type === 'radio') {
                const want = value === true || value === 1
                    || ['true', '1', 'checked', 'yes', 'on'].includes(String(value).toLowerCase());
                if (!!el.checked !== want) {
                    // Native click fires input and change already.
                    el.click();
                    el.dispatchEvent(new Event('blur', {bubbles: true}));
                    return {ok: true, how: 'checked', value: !!el.checked};
                }
                how = 'checked';
            } else if (tag === 'select') {
                const opts = [...el.options];
                const v = String(value).trim();
                const hit = opts.find(o => o.value === v)
                    || opts.find(o => (o.text || '').trim().toLowerCase() === v.toLowerCase());
                if (!hit) return {ok: false, error: 'no option: ' + v};
                el.value = hit.value;
                how = 'option';
            } else if (el.isContentEditable) {
                el.textContent = String(value);
                how = 'richtext';
            } else {
                el.value = String(value);
            }
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            el.dispatchEvent(new Event('blur', {bubbles: true}));
            return {ok: true, how: how,
                    value: (type === 'checkbox' || type === 'radio') ? !!el.checked : el.value};
        };
        const out = {ok: true, fields: {}};
        for (const key of Object.keys(plan)) {
            const el = resolve(key);
            if (!el || !el.isConnected) {
                out.ok = false;
                out.fields[key] = {ok: false, error: 'not found: ' + key};
                continue;
            }
            const r = apply(el, plan[key]);
            if (!r.ok) out.ok = false;
            out.fields[key] = r;
        }
        return out;
    })()"""

    SUBMIT_FORM_JS = r"""(() => {
        // Submit a form by ref/selector (a <form>, a field or a submit
        // button), else the first form on the page. requestSubmit when
        // available; otherwise a submit event and, if not prevented,
        // form.submit(). Never cancels a main-frame navigation.
        const key = %s;
        const refs = window.__browserr_refs || {};
        const pick = (k) => /^e\d+$/.test(k)
            ? refs[k] : document.querySelector(k);
        let el = key ? pick(key) : null;
        if (key && (!el || !el.isConnected)) return {ok: false, error: 'not found: ' + key};
        let form = null;
        if (el && el.tagName === 'FORM') form = el;
        else if (el) form = el.form || (el.closest ? el.closest('form') : null);
        if (!form && document.forms && document.forms.length) form = document.forms[0];
        if (!form) return {ok: false, error: 'no form on the page'};
        const invalid_hint = [];
        for (const f of form.querySelectorAll('input,textarea,select')) {
            if (f.disabled) continue;
            const type = (f.type || '').toLowerCase();
            const val = String(f.value == null ? '' : f.value).trim();
            if (f.hasAttribute('required')
                && ((type === 'checkbox' || type === 'radio') ? !f.checked : val === '')) {
                invalid_hint.push(f.name || f.id || f.tagName.toLowerCase());
            }
        }
        const submitter = (el && (el.tagName === 'BUTTON' || el.tagName === 'INPUT')) ? el : null;
        // Mark that the submit event was seen, so the caller can wait for
        // the dispatch to drain (the click path is asynchronous).
        window.__browserr_submit_seen = 0;
        form.addEventListener('submit', function () {
            window.__browserr_submit_seen = 1;
        }, true);
        // Dispatch the submit event ourselves, then submit natively if the
        // page did not prevent it. servo 0.8's requestSubmit (and a click on
        // a submit button) does NOT reach the page's submit listeners, which
        // silently skips client-side validation; this path is deterministic.
        const ev = new Event('submit', {bubbles: true, cancelable: true});
        const notPrevented = form.dispatchEvent(ev);
        let how = 'submit-event';
        if (notPrevented) {
            if (typeof form.submit === 'function') {
                form.submit();
                how = 'submit-event+native';
            } else if (typeof form.requestSubmit === 'function') {
                form.requestSubmit(submitter || undefined);
                how = 'submit-event+requestSubmit';
            }
        }
        return {ok: true, how: how, action: form.action || '',
                method: (form.method || 'get').toLowerCase(),
                prevented: !notPrevented, invalid_hint: invalid_hint};
    })()"""

    OUTCOME_JS = r"""(() => {
        // What the page says after an action: url/title, alert and
        // aria-live texts (validation errors, toasts) and fields that
        // violate their required/pattern constraints.
        const out = {url: location.href, title: document.title,
                     alerts: [], invalid_fields: []};
        const seen = new Set();
        for (const el of document.querySelectorAll(
                '[role=alert],[aria-live],[role=status],.error,.errors,.alert,[data-error]')) {
            const t = ((el.textContent || '') + ' ' + (el.getAttribute('data-error') || ''))
                .replace(/\s+/g, ' ').trim();
            if (!t || seen.has(t)) continue;
            seen.add(t);
            out.alerts.push(t.slice(0, 200));
            if (out.alerts.length >= 12) break;
        }
        for (const el of document.querySelectorAll('input,textarea,select')) {
            if (el.disabled || el.readOnly) continue;
            const reasons = [];
            const type = (el.type || '').toLowerCase();
            const val = String(el.value == null ? '' : el.value);
            if (el.hasAttribute('required')
                && ((type === 'checkbox' || type === 'radio') ? !el.checked : val.trim() === '')) {
                reasons.push('required');
            }
            const pat = el.getAttribute('pattern');
            if (pat && val) {
                try {
                    if (!new RegExp('^(?:' + pat + ')$').test(val)) reasons.push('pattern');
                } catch (_) {}
            }
            if (reasons.length) {
                out.invalid_fields.push({name: el.name || el.id || '', reasons: reasons});
            }
        }
        return out;
    })()"""

    def fill_form(self, fields, tab: int = 0, timeout=15.0):
        """Write many fields in ONE round trip. `fields` maps a stable ref
        ('e7') or a CSS selector to a value; writes are type-aware (see
        set_field). Blocked while paused. Returns
        {ok, fields: {key: {ok, how, value}}}."""
        self._guard_paused("fill_form")
        js = self.FILL_FORM_JS % json.dumps(fields)
        res = self.eval_js(js, tab, timeout)
        self._log_action("fill_form",
                         targets=sorted((res.get("fields") or {}).keys()),
                         ok=(res or {}).get("ok"))
        return res

    def submit_form(self, ref_or_selector=None, tab: int = 0, timeout=10.0,
                    confirm=False):
        """Submit a form (see the full docstring on the trust-layer
        override; kept for positional compatibility)."""
        return Driver._submit_form_impl(self, ref_or_selector, tab, timeout,
                                        confirm)

    def read_outcome(self, tab: int = 0, timeout=10.0):
        """What the page says after an action: url, title, alert/aria-live
        texts and fields violating required/pattern."""
        return self.eval_js(self.OUTCOME_JS, tab, timeout)

    def wait_network_idle(self, quiet_ms=500, timeout=10.0, tab: int = 0):
        """Block until no fetch/XHR has started for `quiet_ms`. The net
        evidence ring is the clock (no clock skew with the page). Returns
        True when idle, False on timeout."""
        js = ("(() => { const r = window.__browserr_net || []; "
              "const last = r.length ? r[r.length - 1].t : 0; "
              "return Date.now() - last; })()")
        deadline = time.time() + timeout
        while True:
            try:
                quiet = self.eval_js(js, tab, timeout=2.0) or 0
            except TimeoutError:
                quiet = 0
            if quiet >= quiet_ms:
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.1)

    def submit_and_wait(self, ref_or_selector=None, quiet_ms=500,
                        timeout=15.0, tab: int = 0, confirm=False,
                        evidence=False):
        """Submit, settle, then read the outcome. Waits for a navigation to
        land (form POST/GET) or for the network to go quiet (XHR submit).
        confirm is the destructive-action gate (see submit_form); evidence
        additionally saves a screenshot of the landed page to the evidence
        dir. Returns {submit, navigated, outcome[, evidence]}."""
        try:
            before = self.eval_js("location.href", tab)
        except TimeoutError:
            before = ""
        res = self.submit_form(ref_or_selector, tab, confirm=confirm)
        deadline = time.time() + timeout
        navigated = False
        while time.time() < deadline:
            time.sleep(0.2)
            try:
                now = self.eval_js("location.href", tab, timeout=2.0)
            except TimeoutError:
                continue
            if now and now != before:
                navigated = True
                break
            if self.wait_network_idle(quiet_ms=quiet_ms, timeout=0.5, tab=tab):
                break
        self.wait_network_idle(quiet_ms=quiet_ms, timeout=3.0, tab=tab)
        outcome = self.read_outcome(tab)
        out = {"submit": res, "navigated": navigated, "outcome": outcome}
        if evidence and self.evidence_dir:
            try:
                out["evidence"] = self.evidence(mode="plain", tab=tab)
            except (TimeoutError, EngineDeadError):
                out["evidence"] = {"error": "screenshot failed"}
        return out

    HANDOFF_JS = r"""(() => {
        // Things a human must do: CAPTCHAs, one-time codes, passwords with
        // no stored credential, payment fields, and cookie/consent walls.
        // Returns {handoff: bool, reasons: [{kind, detail}], paused_hint}.
        const reasons = [];
        const host = location.hostname;
        const iframe = (sel) => {
            for (const f of document.querySelectorAll('iframe')) {
                if (f.src && f.src.includes(sel)) return f.src;
            }
            for (const s of document.scripts) {
                if (s.src && s.src.includes(sel)) return s.src;
            }
            return null;
        };
        const captcha = iframe('recaptcha') || iframe('hcaptcha')
            || iframe('turnstile') || iframe('grecaptcha');
        if (captcha) reasons.push({kind: 'captcha', detail: captcha.slice(0, 150)});
        for (const i of document.querySelectorAll('input')) {
            const t = (i.type || '').toLowerCase();
            const ac = (i.getAttribute('autocomplete') || '').toLowerCase();
            if (!i.value && (ac.includes('one-time-code') || ac.includes('otp')
                    || (t === 'text' && /^\d{4,8}$/.test(i.getAttribute('pattern') || '')
                        && (i.name || i.id || '').toLowerCase().match(/otp|code|token/)))) {
                reasons.push({kind: 'otp',
                              detail: (i.name || i.id || 'input').slice(0, 80)});
            }
            if (!i.value && t === 'password'
                    && (ac.includes('current-password') || !ac)) {
                reasons.push({kind: 'password',
                              detail: (i.name || i.id || 'input').slice(0, 80)});
            }
            if (!i.value && (ac.includes('cc-number') || ac.includes('card-number')
                    || ac.includes('cc-csc') || ac.includes('card-cvc'))) {
                reasons.push({kind: 'payment',
                              detail: (i.name || i.id || 'input').slice(0, 80)});
            }
        }
        const consent = document.querySelector(
            '[role=dialog] button, [class*=consent] button, [id*=consent] button');
        if (consent && document.querySelector('[class*=cookie], [id*=cookie]')) {
            reasons.push({kind: 'consent',
                          detail: (consent.textContent || '').trim().slice(0, 80)});
        }
        return {handoff: reasons.length > 0, reasons: reasons,
                paused_hint: reasons.length
                    ? 'human needed for: ' + reasons.map(r => r.kind).join(', ')
                    : ''};
    })()"""

    def detect_handoff(self, tab: int = 0, timeout=10.0):
        """Scan the page for things a human must do (CAPTCHA, OTP, password
        without a stored credential, payment fields, cookie consent).
        Returns {handoff, reasons, paused_hint}."""
        return self.eval_js(self.HANDOFF_JS, tab, timeout)

    # -- trust layer: evidence, action log, credentials, confirm gate -------

    @staticmethod
    def _host_of(url: str) -> str:
        """host[:port] of a URL; empty string when unparseable."""
        m = re.match(r"^[a-z][a-z0-9+.-]*://([^/?#]+)", (url or "").lower())
        return m.group(1) if m else ""

    def enable_evidence(self, directory) -> None:
        """Set the directory annotated/plain evidence shots are written to
        (created on demand) and where the append-only action log lives."""
        self.evidence_dir = directory
        os.makedirs(directory, exist_ok=True)
        self.action_log_path = os.path.join(directory, "actions.jsonl")

    def _shot_file(self, directory, kind):
        path = os.path.join(directory, f"{kind}-{int(time.time() * 1000)}.png")
        w, h, bgra = self.screenshot()
        save_png(path, w, h, bgra)
        return path

    def evidence(self, mode="plain", tab: int = 0, timeout=20.0):
        """Write a screenshot of the current state to the evidence dir.
        mode 'plain' saves the frame; 'annotated' adds numbered boxes and
        returns the item list. Returns {path, ...} or an error dict when
        no evidence dir is configured."""
        if not self.evidence_dir:
            return {"error": "no evidence dir; call enable_evidence first"}
        if mode == "annotated":
            path = os.path.join(
                self.evidence_dir, f"ev-{int(time.time() * 1000)}.png")
            res = self.screenshot_annotated(path, tab, timeout)
            self._log_action("evidence", mode="annotated",
                             path=res.get("path"))
            return res
        out = {"path": self._shot_file(self.evidence_dir, "ev")}
        self._log_action("evidence", mode="plain", path=out["path"])
        return out

    def action_log(self, limit: int = 100):
        """Read back the append-only action log (newest last): a list of
        {t, kind, ...} JSON lines, plus the path. Best effort; returns
        {entries: [], path} when no log is configured or the file is
        unreadable, never raises."""
        entries = []
        path = self.action_log_path
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entries.append(json.loads(line))
                        except ValueError:
                            pass  # a torn line from a concurrent append
            except OSError:
                pass
        return {"path": path, "entries": entries[-max(1, min(limit, 1000)):]}

    def _log_action(self, kind: str, **fields) -> None:
        """Append one JSON line to the action log (best effort; never raises)."""
        if not self.action_log_path:
            return
        try:
            line = {"t": round(time.time(), 3), "kind": kind}
            line.update(fields)
            with self._log_lock:
                with open(self.action_log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(line, default=str) + "\n")
        except OSError:
            pass

    def set_credentials(self, origin_or_host, user, password) -> None:
        """Store credentials for a host (or URL; only the host[:port] is
        kept). Used automatically when the engine reports an HTTP auth
        challenge for that host; never written to the action log."""
        host = self._host_of(origin_or_host) if "://" in origin_or_host \
            else origin_or_host
        self._credentials[host] = (user, password)

    def start_auto_auth(self) -> None:
        """Answer engine auth challenges from stored credentials, in the
        background. Unknown hosts fail closed (challenge dropped = no
        credentials sent). Idempotent."""
        if self._auth_thread is not None:
            return

        def loop():
            while not self.dead:
                try:
                    ev = self.wait_auth(timeout=1.0)
                except (TimeoutError, EngineDeadError):
                    continue
                rid = ev.get("id")
                self._auth_seen.add(rid)
                host = self._host_of(ev.get("url") or "")
                creds = self._credentials.get(host)
                # Drop-on-no-creds: answering with nothing fails closed.
                self.auth_answer(rid, creds[0] if creds else None,
                                 creds[1] if creds else None)
                self._log_action("auth", host=host,
                                  answered=bool(creds))

        self._auth_thread = threading.Thread(target=loop, daemon=True)
        self._auth_thread.start()

    def _submit_form_impl(self, ref_or_selector=None, tab: int = 0,
                          timeout=10.0, confirm=False):
        """Submit a form, with a fail-closed destructive-action gate.

        When the page shows payment or captcha conditions (detect_handoff),
        submitting needs confirm=True - i.e. a human said yes - otherwise
        NeedsConfirmError is raised and nothing is submitted. Returns
        {ok, how, action, method, prevented, invalid_hint}."""
        self._guard_paused("submit_form")
        if not confirm:
            try:
                h = self.detect_handoff(tab, timeout=5.0)
            except (TimeoutError, EngineDeadError):
                h = {}
            risky = {r.get("kind") for r in (h.get("reasons") or [])}
            if {"payment", "captcha"} & risky:
                # A refused destructive attempt belongs in the audit log as
                # much as a performed one.
                self._log_action("submit", target=ref_or_selector,
                                 confirm=confirm, refused=True,
                                 reasons=sorted(risky))
                raise NeedsConfirmError(
                    "page shows " + "/".join(sorted(risky))
                    + " conditions; call submit_form(confirm=True) after a "
                      "human confirms")
        self._log_action("submit", target=ref_or_selector, confirm=confirm)
        js = self.SUBMIT_FORM_JS % json.dumps(ref_or_selector)
        res = self.eval_js(js, tab, timeout)
        if not res or not res.get("ok"):
            return res
        # requestSubmit dispatches synchronously; the click fallback is
        # asynchronous, so wait until the page has seen the submit event
        # (or the document went away because a navigation started).
        deadline = time.time() + 2.0
        while time.time() < deadline:
            try:
                seen = self.eval_js("window.__browserr_submit_seen", tab, timeout=1.0)
            except TimeoutError:
                break
            if seen:
                break
            time.sleep(0.05)
        return res

    # -- net evidence / frames ------------------------------------------------

    def net_evidence(self, limit=200, tab: int = 0, timeout=10.0):
        """Recent page network activity (fetch/XHR) from the ring buffer
        the document-start user script keeps on window.__browserr_net.
        Runs in every frame; this reads the top frame's ring."""
        js = ("JSON.stringify((window.__browserr_net||[]).slice(-%d))" % limit)
        raw = self.eval_js(js, tab, timeout)
        if isinstance(raw, str):
            return json.loads(raw)
        return raw or []


if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    out = sys.argv[2] if len(sys.argv) > 2 else "browserr-shot.png"
    d = Driver(log="browserr-driver.log")
    try:
        d.navigate(url)
        d.wait_load()
        print("url:   ", d.eval_js("location.href"))
        print("title: ", d.eval_js("document.title"))
        w, h, bgra = d.screenshot()
        save_png(out, w, h, bgra)
        print(f"shot:  {w}x{h} -> {out}")
    finally:
        d.close()
