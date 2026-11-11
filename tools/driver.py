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
                 width=1200, height=760, log=None, ephemeral=False):
        self._init_common(port, log=log)
        self.width = width
        self.height = height
        self.ephemeral = ephemeral

        engine_path = engine_path or os.environ.get("BROWSERR_ENGINE", ENGINE_DEBUG)
        args = [engine_path]
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
            raise ConnectionError(f"engine did not answer on {self.port}: {last}")

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
        self._cond = threading.Condition()
        self._stop = False
        self.proc = None
        self.sock = None
        self._log = open(log, "w") if log else None
        self.width = None
        self.height = None
        self.dead = False           # set by the reader thread on socket loss

    # -- protocol ---------------------------------------------------------

    def cmd(self, obj: dict) -> None:
        if self.dead:
            raise EngineDeadError("engine socket gone (cmd dropped)")
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
                        self.events.append(json.loads(payload))
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

    def wait_for(self, pred, timeout=10.0):
        """Wait until pred(event) holds for some event, return that event.
        Raises EngineDeadError as soon as the reader notices the engine
        socket is gone (no hanging until the timeout)."""
        deadline = time.time() + timeout
        with self._cond:
            while True:
                for e in reversed(self.events):
                    if pred(e):
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
        controlled inputs accept the change. Returns True on success."""
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
        """Wait for the next held-request decision event; returns it."""
        return self.wait_for(
            lambda e: e.get("ev") == "request"
            and (tab is None or e.get("tab") == tab)
            and (url_contains is None or url_contains in e.get("url", "")),
            timeout)

    def request_answer(self, req_id, action: str, status=200, body="",
                       content_type="text/plain"):
        """Answer a held request: "allow" passes it through, "deny" fulfills
        it with a synthetic 403, "respond" fulfills it with status/body."""
        self.cmd({"cmd": "request", "tab": 0, "id": req_id, "action": action,
                  "status": status, "body": body, "content_type": content_type})

    def wait_auth(self, tab=None, timeout=15.0):
        """Wait for the next auth challenge event
        {"ev":"auth","id","tab","url","proxy"}; returns it."""
        return self.wait_for(
            lambda e: e.get("ev") == "auth"
            and (tab is None or e.get("tab") == tab),
            timeout)

    def auth_answer(self, req_id, user=None, password=None):
        """Answer an auth challenge. Without credentials the challenge is
        failed closed (no credentials sent)."""
        args = {"cmd": "auth", "tab": 0, "id": req_id}
        if user is not None and password is not None:
            args.update({"user": user, "pass": password})
        self.cmd(args)

    def wait_filepicker(self, tab=None, timeout=15.0):
        """Wait for the next file-picker event
        {"ev":"filepicker","id","tab","multiple"}; returns it."""
        return self.wait_for(
            lambda e: e.get("ev") == "filepicker"
            and (tab is None or e.get("tab") == tab),
            timeout)

    def files_answer(self, req_id, paths):
        """Answer a file picker with a list of paths (empty/None = cancel)."""
        self.cmd({"cmd": "files", "tab": 0, "id": req_id,
                 "paths": list(paths) if paths else []})

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
        // rects and a usable CSS selector, so agents can act without
        // screenshots (feeds click_element/set_value).
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
        const out = {title: document.title, headings: [], buttons: [],
                     inputs: [], selects: [], links: [], images: []};
        const vis = (el) => {
            const s = getComputedStyle(el);
            return s.display !== 'none' && s.visibility !== 'hidden';
        };
        for (const h of document.querySelectorAll('h1,h2,h3,h4')) {
            if (vis(h)) out.headings.push({level: +h.tagName[1], text: h.textContent.trim().slice(0, 200)});
        }
        for (const b of document.querySelectorAll('button, [role=button], input[type=submit], input[type=button]')) {
            if (!vis(b)) continue;
            const r = rect(b);
            if (!r.w || !r.h) continue;
            out.buttons.push({text: (b.value || b.textContent).trim().slice(0, 80),
                              selector: sel(b), ...r});
        }
        for (const i of document.querySelectorAll('input:not([type=submit]):not([type=button]):not([type=hidden]), textarea')) {
            if (!vis(i)) continue;
            const r = rect(i);
            if (!r.w && !r.h) continue;
            out.inputs.push({type: i.type || 'text', name: i.name || '', id: i.id || '',
                             placeholder: i.placeholder || '', selector: sel(i), ...r});
        }
        for (const s of document.querySelectorAll('select')) {
            if (!vis(s)) continue;
            const r = rect(s);
            out.selects.push({name: s.name || '', id: s.id || '', selector: sel(s),
                              options: [...s.options].map(o2 => o2.value).slice(0, 50), ...r});
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

    def read_page(self, tab: int = 0, timeout=15.0):
        """Extract the page as (title, markdown-ish text): links become
        [label](href), paragraphs get blank-line separation. Token-cheap."""
        title, text = self.eval_js(self.READ_JS, tab, timeout)
        return title, text

    def read_structure(self, tab: int = 0, timeout=15.0):
        """Structured page inventory: headings, buttons, inputs, selects,
        links and images, each with a viewport rect and a usable CSS selector
        (feeds click_element/set_value). Richer than read_page; use when the
        agent needs to locate and act on specific UI."""
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
        Returns True if a field was found and filled."""
        js = ("((()=>{const el=document.getElementsByName(%s)[0]"
              "||document.getElementById(%s);"
              "if(!el)return false;el.focus();el.value=%s;"
              "el.dispatchEvent(new Event('input',{bubbles:true}));"
              "el.dispatchEvent(new Event('change',{bubbles:true}));return true;})())"
              % (json.dumps(name_or_id), json.dumps(name_or_id), json.dumps(value)))
        return bool(self.eval_js(js, tab, timeout))


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
