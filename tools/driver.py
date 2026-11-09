"""Browserr automation driver (engine-level, like CDP for chrome.exe).

Speaks the engine's loopback JSON protocol directly: spawn engine.exe, then
navigate, eval JS (results returned), inject input, and take screenshots.

    from driver import Driver
    d = Driver()
    try:
        d.navigate("https://example.com")
        d.wait_load()
        print(d.eval_js("document.title"))
        w, h, bgra = d.screenshot()
        d.save_png("shot.png", w, h, bgra)
    finally:
        d.close()

Note: the engine accepts exactly one connection (the chrome's), so a Driver
talks to a headless engine it spawns itself - not to a running Browserr UI.
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
    """Drives a headless Browserr engine process over its automation protocol."""

    def __init__(self, engine_path=None, profile_dir=None, port=7411,
                 width=1200, height=760, log=None):
        self.port = port
        self.width = width
        self.height = height
        self.events = []            # all EVT1 events, newest last
        self.shots = []             # (w, h, bgra) screenshots, newest last
        self._next_id = 1
        self._cond = threading.Condition()
        self._stop = False
        self.proc = None
        self.sock = None

        engine_path = engine_path or os.environ.get(
            "BROWSERR_ENGINE",
            r"D:/Code/2026/Browserr/engine/target/debug/engine.exe")
        args = [engine_path]
        if profile_dir:
            args.append(f"--profile-dir={profile_dir}")
        self._log = open(log, "w") if log else None
        self.proc = subprocess.Popen(args, stderr=self._log or subprocess.DEVNULL)

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

    # -- protocol ---------------------------------------------------------

    def cmd(self, obj: dict) -> None:
        self.sock.sendall((json.dumps(obj) + "\n").encode())

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
            except OSError:
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
                    return  # unknown stream: bail out
            except OSError:
                return

    def wait_for(self, pred, timeout=10.0):
        """Wait until pred(event) holds for some event, return that event."""
        deadline = time.time() + timeout
        with self._cond:
            while True:
                for e in reversed(self.events):
                    if pred(e):
                        return e
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

    def close(self) -> None:
        self._stop = True
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        if self.proc:
            self.proc.kill()
        if self._log:
            self._log.close()


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
