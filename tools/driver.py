"""Browserr automation driver (like CDP for chrome.exe, but our own protocol).

Two modes sharing one API:

Headless (spawn a bare engine):
    from driver import Driver
    d = Driver()
    d.navigate("https://example.com"); d.wait_load(); d.wait_ready()
    print(d.eval_js("document.title"))

Computer use (drive a real browser window; the UI keeps working):
    chrome.exe --driver-port=7412 --profile=test
    from driver import Driver
    d = Driver.attach(7412)
    d.navigate("https://example.com"); d.wait_ready()
    w, h, bgra = d.screenshot()   # what the window shows

Capabilities: navigate, wait_load/wait_ready, eval_js (results returned as
JSON values), screenshot (BGRA buffer, save_png helper), click, move, wheel,
key, type_text, new_tab, activate. Input coordinates are page-relative; in
attach mode the page starts below the 40px toolbar.

The headless engine accepts exactly one connection (the chrome's), which is
why the driver spawns its own engine there. The chrome's driver port accepts
multiple drivers and mirrors engine events/screenshots to all of them.
"""
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
        chrome.exe --driver-port=N window instead - same API, but it drives
        the visible browser (and the UI keeps working).
    """

    @classmethod
    def attach(cls, port=7412, log=None):
        """Attach to a running Browserr window started with --driver-port=N."""
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
        threading.Thread(target=self._read_loop, daemon=True).start()
        return self

    def __init__(self, engine_path=None, profile_dir=None, port=7411,
                 width=1200, height=760, log=None, ephemeral=False):
        self._init_common(port, log=log)
        self.width = width
        self.height = height
        self.ephemeral = ephemeral

        engine_path = engine_path or os.environ.get(
            "BROWSERR_ENGINE",
            r"D:/Code/2026/Browserr/engine/target/debug/engine.exe")
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
        for _ in range(100):
            try:
                self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=1)
                break
            except OSError as e:
                last = e
                time.sleep(0.1)
        if self.sock is None:
            self.proc.kill()
            raise ConnectionError(f"engine did not listen on {self.port}: {last}")

        self.cmd({"cmd": "resize", "w": width, "h": height})
        threading.Thread(target=self._read_loop, daemon=True).start()

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

    def read_page(self, tab: int = 0, timeout=15.0):
        """Extract the page as (title, markdown-ish text): links become
        [label](href), paragraphs get blank-line separation. Token-cheap."""
        title, text = self.eval_js(self.READ_JS, tab, timeout)
        return title, text

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
