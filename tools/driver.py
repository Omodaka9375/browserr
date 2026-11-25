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
import base64
import json
import random
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib
import os
# Sibling modules (driver_js, annotate) sit next to this file; not
# every importer puts TOOLS_DIR on sys.path, so do it ourselves.
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

FRM1 = 0x46524D31
EVT1 = 0x45565431
SHT1 = 0x53485431
FRM2 = 0x46524D32


def _is_android():
    """True inside Termux (Android): the engine needs its libc++_shared.so
    on LD_LIBRARY_PATH and the ephemeral profile dir must live under
    $PREFIX/tmp (Bionic has no /tmp)."""
    return os.path.isdir("/data/data/com.termux/files/usr")


def _bind_child_to_parent_lifetime(proc):
    """Orphan-proofing: make the OS kill the engine child when THIS
    process dies, however it dies (crash, kill, KAI tool timeout). The
    engine's own shutdown paths (client disconnect, driver stop) run on
    the servo main loop and never fire while a page wedges it - a killed
    MCP server then leaked a wedged engine forever, holding port 7411
    and its ephemeral profile (the "orphaned active instances" seen in
    the field).

    Windows: assign the child to a Job with
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; the OS closes the job handle at
    our death, terminating the engine even mid-wedge. Returns the job
    handle (the caller must keep it open until death or close()).
    Linux: returns a preexec_fn setting PR_SET_PDEATHSIG=SIGKILL.
    macOS: no equivalent; socket-EOF covers the graceful case only.

    Windows call form: _bind_child_to_parent_lifetime(proc) -> handle.
    Linux call form: _bind_child_to_parent_lifetime(None) -> preexec_fn.
    """
    if _os.name == "nt":
        if proc is None:
            return None  # windows: called WITH the proc after Popen
        import ctypes
        from ctypes import wintypes

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [(f, ctypes.c_ulonglong) for f in
                        ("ReadOperationCount", "WriteOperationCount",
                         "OtherOperationCount", "ReadTransferCount",
                         "WriteTransferCount", "OtherTransferCount")]

        class _BASIC_LIMITS(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.POINTER(wintypes.ULONG)),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class _EXT_LIMITS(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _BASIC_LIMITS),
                        ("IOInfo", _IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9
        kernel32 = ctypes.windll.kernel32
        h = kernel32.CreateJobObjectW(None, None)
        if not h:
            return None
        info = _EXT_LIMITS()
        info.BasicLimitInformation.LimitFlags = \
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
                h, JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(h)
            return None
        if not kernel32.AssignProcessToJobObject(h, proc._handle):
            kernel32.CloseHandle(h)
            return None
        return h  # keep open for our lifetime; the OS closes it on death
    if sys.platform.startswith("linux") and proc is None:
        import ctypes

        def _pdeathsig():
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            libc.prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
        return _pdeathsig
    return None


class EngineDeadError(ConnectionError):
    """The engine process/socket died mid-session (crash, hang, kill).

    Raised by wait_for instead of hanging until the timeout; agents catch it
    (or let it bubble) and retry with a fresh Driver.
    """


class EvalError(RuntimeError):
    """A page script evaluation failed (JS exception, CSP block, or a
    rejected promise). Carries the engine's error text; never silently
    returned as a result dict.
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


# Pixel annotation lives in annotate.py (pure image helpers).
from annotate import draw_number, draw_rect, save_png


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
        args = [engine_path, f"--width={width}", f"--height={height}"]
        if port != 7411:
            # Multi-engine hosts run several engines side by side; the
            # default port is shared for backward compatibility.
            args.append(f"--port={port}")
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
        # Orphan guard: the engine must die with THIS process even when
        # a wedged page keeps it from noticing the socket close. Windows:
        # job handle (kept on self, closed by the OS at our death).
        # Linux: PDEATHSIG preexec_fn.
        if _os.name == "nt":
            preexec = None
        else:
            preexec = _bind_child_to_parent_lifetime(None)
        # Android (Termux): Bionic does not search the binary's own
        # directory for libc++_shared.so, so put the engine dir on
        # LD_LIBRARY_PATH before spawning.
        env = None
        if _is_android():
            libdir = os.path.dirname(os.path.abspath(engine_path))
            env = dict(os.environ)
            existing = env.get("LD_LIBRARY_PATH", "")
            env["LD_LIBRARY_PATH"] = (libdir + os.pathsep + existing
                                      if existing else libdir)
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=self._log or subprocess.DEVNULL,
                                    preexec_fn=preexec, env=env)
        self._parent_guard = _bind_child_to_parent_lifetime(self.proc)

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
                req = self._alloc_id()
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

    def _alloc_id(self):
        with self._id_lock:
            n = self._next_id
            self._next_id += 1
            return n

    def _init_common(self, port, log=None):
        """Shared state for both modes (headless spawn vs attach)."""
        self.port = port
        self.events = []            # all EVT1 events, newest last
        self.shots = []             # (w, h, bgra) screenshots, newest last
        self.native_frames = []     # FRM2 patches: (id, rect, format, flags, data)
        self.native_frame_total_bytes = 0
        self._next_id = 1
        self._id_lock = threading.Lock()  # MCP fast-lane threads allocate
        # eval/ping ids concurrently; a race could duplicate an id and make
        # one caller receive the other's answer
        self._event_seq = 0         # driver-local ordering for event cursors
        self._answered_requests = set()
        self._answered_auth = set()
        self._answered_filepickers = set()
        self._cond = threading.Condition()
        self._send_lock = threading.Lock()  # concurrent cmd() callers (MCP
        # fast lane) must not interleave bytes on the engine socket
        self._stop = False
        self.proc = None
        self.sock = None
        self._log = open(log, "w") if log else None
        self.width = None
        self.height = None
        self.dead = False           # set by the reader thread on socket loss
        self.unreadable_events = 0  # events the reader could not parse
        self._paused = None        # reason string while a human has control
        self._handoff_seen = False
        self._nav_target = {}      # tab -> last navigate() url (for wait_ready)
        self._nav_marker = {}      # tab -> generation stamped on the OLD doc
                                   # (wait_ready must not return ready on the
                                   # document we are leaving)

        # Trust layer state: evidence dir, action log, stored credentials.
        self.evidence_dir = None
        self.action_log_path = None
        self._credentials = {}     # host[:port] -> (user, password)
        self._auth_rejected = {}   # host -> ((user, pass), time of answer)
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
        req = self._alloc_id()
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
            data = (json.dumps(obj) + "\n").encode()
            with self._send_lock:
                self.sock.sendall(data)
        except OSError as e:
            self._mark_dead()
            raise EngineDeadError(f"send failed: {e}")

    def _read_exact(self, n: int) -> bytes:
        if n < 0 or n > 96 * 1024 * 1024:
            raise ConnectionError(f"frame payload implausible size: {n}")
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
                if magic == FRM2:
                    # Header is 48 bytes: first 20 contain magic, request id,
                    # payload length and x. Remaining 28: y/w/h, format,
                    # flags, and an eight-byte frame id.
                    x = struct.unpack("<I", hdr[16:20])[0]
                    y, w, h, fmt, flags = struct.unpack("<5I", self._read_exact(20))
                    self._read_exact(8)  # frame id (diagnostic only)
                    payload = self._read_exact(a)
                    with self._cond:
                        # Cap total native-frame bytes: a hostile/chatty
                        # engine could otherwise grow this indefinitely.
                        if self.native_frame_total_bytes + len(payload) > 256 * 1024 * 1024:
                            self.native_frames.clear()
                            self.native_frame_total_bytes = 0
                            self.unreadable_events += 1
                        else:
                            self.native_frame_total_bytes += len(payload)
                            self.native_frames.append((hdr[4:12], (x, y, w, h),
                                                       fmt, flags, payload))
                        self._cond.notify_all()
                elif magic == FRM1:            # live frame: skip
                    self._read_exact(a * b * 4)
                elif magic == SHT1:          # screenshot frame
                    data = self._read_exact(a * b * 4)
                    with self._cond:
                        self.shots.append((a, b, data))
                        self._cond.notify_all()
                elif magic == EVT1:
                    payload = self._read_exact(a)
                    try:
                        # strict=False: engines up to 1.0.0 put a page's
                        # raw control characters inside event strings.
                        event = json.loads(payload, strict=False)
                    except ValueError:
                        # One unreadable event must not end the reader:
                        # every later wait would then hang until timeout.
                        # A dropped reply still times out its caller, so
                        # say that it happened.
                        self.unreadable_events += 1
                        sys.stderr.write(
                            "browserr driver: dropped an unreadable event "
                            f"({len(payload)} bytes)\n")
                        continue
                    with self._cond:
                        self._event_seq += 1
                        event["_driver_seq"] = self._event_seq
                        self.events.append(event)
                        if len(self.events) > 2000:
                            # Cap the event log: a wedged/chatty engine or
                            # a hostile loop would otherwise grow this
                            # list without bound.
                            del self.events[:len(self.events) - 2000]
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

    def _arm_nav_marker(self, tab: int) -> None:
        """Stamp the CURRENT document with a unique string marker.

        wait_ready must not return ready on the document we are LEAVING
        (it is already readyState-complete), so the string recorded here is
        compared against the document later seen by wait_ready: a different
        value means a genuinely new document. Using a string (not a number)
        avoids JSON float formatting differences (1.0 vs '1'). None (the
        eval failed - page wedged or not scriptable) disables the check and
        keeps the classic readyState-only behavior.
        """
        token = f"nav-{self._alloc_id()}"
        try:
            got = self.eval_js(
                f"(window.__browserr_int.navgen = {json.dumps(token)})",
                tab, 2.0)
            self._nav_marker[tab] = str(got) if got == token else None
        except (TimeoutError, EvalError, EngineDeadError):
            self._nav_marker[tab] = None

    def navigate(self, url: str, tab: int = 0) -> None:
        self._guard_paused("navigate")
        self._log_action("navigate", url=url)
        self._nav_target[tab] = url
        self._arm_nav_marker(tab)
        self.cmd({"cmd": "navigate", "url": url, "tab": tab})

    def back(self, tab: int = 0) -> None:
        """Go back in the tab's per-tab history. Same document swap as
        navigate, so the wait_ready marker is armed the same way."""
        self._guard_paused("back")
        self._log_action("back", tab=tab)
        self._arm_nav_marker(tab)
        self.cmd({"cmd": "back", "tab": tab})

    def forward(self, tab: int = 0) -> None:
        """Go forward in the tab's per-tab history."""
        self._guard_paused("forward")
        self._log_action("forward", tab=tab)
        self._arm_nav_marker(tab)
        self.cmd({"cmd": "forward", "tab": tab})

    def scroll(self, dx=0, dy=0, x=10, y=10, tab: int = 0) -> None:
        """Wheel-scroll the page (positive dy scrolls down, positive dx
        scrolls right). Servo's wheel event uses content-movement sign
        (negative dy moves content up / scrolls view down), so negate."""
        self.cmd({"cmd": "wheel", "tab": tab, "dx": -dx, "dy": -dy,
                  "x": x, "y": y})

    def resize(self, w, h, tab: int = 0) -> None:
        """Resize the viewport (the active tab; others on activate)."""
        self.cmd({"cmd": "resize", "tab": tab, "w": int(w), "h": int(h)})

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

        Servo also commits navigations through a transient about:blank
        document whose readyState is instantly "complete", so readyState
        alone can report ready while the REAL document is still loading
        (every read/eval then sees an empty page). Ready therefore requires
        readyState complete AND a live document that is not about:blank -
        unless the caller navigated to about:blank on purpose.

        AND a genuinely NEW document: the document we navigate AWAY from is
        already readyState-complete, so without this check wait_ready could
        return ready on the old document right after a navigation commits
        (reads/evals would then land on stale content). navigate/back/
        forward stamp the old document with a generation counter; ready
        requires the document under us to carry a different one. When the
        stamp failed (wedged or unscriptable page), the check degrades to
        the classic readyState behavior.
        """
        deadline = time.time() + timeout
        target = self._nav_target.get(tab)
        marker = self._nav_marker.get(tab)
        while time.time() < deadline:
            try:
                state = self.eval_js(
                    "location.href + '\\n' + document.readyState + '\\n' + "
                    "((window.__browserr_int && window.__browserr_int.navgen !== undefined) "
                    "? window.__browserr_int.navgen : '-')",
                    tab, timeout=1.5)
                href, _, rest = (state or "").partition("\n")
                ready, _, gen = rest.partition("\n")
                new_doc = (marker is None or gen != str(marker))
                if (ready == "complete" and new_doc
                        and (href != "about:blank" or target == "about:blank")):
                    return
            except (TimeoutError, EvalError):
                # dropped evals (early) and mid-replacement document
                # errors are both transient while the page loads
                pass
            time.sleep(0.2)
        raise TimeoutError("page never became ready")

    def eval_js(self, js: str, tab: int = 0, timeout=10.0):
        """Evaluate JS in the page and return the (JSON-friendly) result.

        The ENGINE wraps every driver script in its own injected wrapper
        (EVAL_WRAPPER_JS in the engine): no page-side eval() is used, so
        strict-CSP sites (no unsafe-eval) cannot block the call, and
        thenable results are parked in the engine's hidden, non-enumerable
        window slot (window.__browserr_int.evals) and polled here until
        they settle. Statement scripts (no completion value) are retried
        raw by the engine automatically; on the raw path servo does not
        await promises (a thenable serializes as {}). Promises should
        resolve to plain JSON values; complex objects serialize only their
        enumerable properties."""
        # The engine receives the raw script; it wraps and parks itself.
        # No page-side code from this process ever touches page globals.
        req = self._alloc_id()
        self.cmd({"cmd": "eval", "tab": tab, "js": js, "id": req})
        ev = self.wait_for(
            lambda e: e.get("ev") == "evalresult" and e.get("id") == req, timeout)
        value = ev.get("value")
        if isinstance(value, dict) and "__error" in value:
            raise EvalError(str(value["__error"]))
        if not (isinstance(value, dict) and "__pending" in value):
            # Either the wrapper returned {__value:..} (already JSON), or
            # the engine retried raw and returned the direct value.
            if isinstance(value, dict) and "__value" in value:
                return value["__value"]
            return value
        # The wrapper parked a promise: poll its slot until it settles
        # (each poll is a tiny sync eval through the engine).
        slot = value["__pending"]
        poll = ("(function(){var s=window.__browserr_int&&"
                "window.__browserr_int.evals[" + str(slot) + "];"
                "if(!s||s.p){return null;}"
                "delete window.__browserr_int.evals[" + str(slot) + "];"
                "return s.e!==null?{__error:s.e}:{__value:s.v};})()")
        deadline = time.time() + timeout
        while True:
            req = self._alloc_id()
            self.cmd({"cmd": "eval", "tab": tab, "js": poll, "id": req})
            try:
                ev = self.wait_for(
                    lambda e: (e.get("ev") == "evalresult"
                               and e.get("id") == req), 2.0)
            except TimeoutError:
                pass  # page busy; keep polling until the deadline
            else:
                r = ev.get("value")
                if isinstance(r, str):
                    try:
                        r = json.loads(r)
                    except (ValueError, TypeError):
                        r = None
                if isinstance(r, dict) and "__error" in r:
                    # the parked promise rejected
                    raise EvalError(str(r["__error"]))
                if isinstance(r, dict) and "__value" in r:
                    return r["__value"]
                if r is not None:
                    return r
            if time.time() >= deadline:
                raise TimeoutError("eval promise did not settle in time")

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

    def native_frame(self, tab=0, format="png", stream=0, cell_w=10,
                     cell_h=20, force=False, timeout=15.0):
        """Return native compressed frame rectangles.

        Each stream retains its own reference frame in the engine. Subsequent
        calls return only dirty cell-aligned rectangles; unchanged frames
        return an empty list. Format is 'png' or 'sixel'. This is the fast
        path for interactive renderers; screenshot() stays the SDK's lossless
        full-frame BGRA API.
        """
        req = self._alloc_id()
        self.cmd({"cmd": "frame", "tab": tab, "id": req, "format": format,
                  "stream": stream, "cell_w": cell_w, "cell_h": cell_h,
                  "force": force})
        deadline = time.time() + timeout
        rb = req.to_bytes(8, "little")
        patches = []
        with self._cond:
            while True:
                # Take this request's whole batch in one pass. remove()
                # inside a for-loop over self.native_frames shifts the list
                # under the iterator and skips the packet right after each
                # hit: a coalesced [P1, P2, END] wake returned only P1, and
                # P2 stayed forever (request ids never repeat, so the
                # timeout purge below never matches it either).
                batch = [p for p in self.native_frames if p[0] == rb]
                if batch:
                    self.native_frames = [p for p in self.native_frames
                                          if p[0] != rb]
                    patches += [p for p in batch if not (p[3] & 2)]
                    if any(p[3] & 2 for p in batch):
                        return patches
                if self.dead:
                    raise EngineDeadError("engine socket gone (native frame)")
                remaining = deadline - time.time()
                if remaining <= 0:
                    # Do not leak this request's patches into later scans.
                    self.native_frames = [p for p in self.native_frames if p[0] != rb]
                    raise TimeoutError("native frame never completed")
                self._cond.wait(min(remaining, 0.25))

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
        for group in ("buttons", "inputs", "selects", "links"):
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

    def element_rect(self, selector, tab: int = 0, timeout=10.0, scroll=True):
        """Return the viewport rect {x, y, w, h} of the first element
        matching a CSS selector, or None while it does not exist yet.
        scroll=True also scrolls the element into view (click targeting);
        polling callers pass scroll=False - scrollIntoView forces a full
        layout, which on a large slow SPA both costs seconds per poll and
        wedges the page's script thread (the Oracle 0.7.1 wedge)."""
        js = ("(function(){var s=" + self._js_selector(selector) + ";"
          "var e=document.querySelector(s);if(!e){return null;}"
          + ("e.scrollIntoView({block:'center',inline:'nearest'});"
             if scroll else "")
          + "var r=e.getBoundingClientRect();"
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

    def _humanized_click(self, rect, tab: int = 0):
        """Click inside an element the way a person does: a few pointer
        moves toward a jittered point inside the element (never exactly
        the center, never a perfectly straight path), small pauses, then
        down/up. Keeps sites that flag instant center-clicks from
        noticing automation; deliberately simple - no bezier curves."""
        w, h = max(rect["w"], 1.0), max(rect["h"], 1.0)
        # Jitter inside the central 60% of the element.
        jx = w * (0.2 + 0.6 * random.random()) - w / 2
        jy = h * (0.2 + 0.6 * random.random()) - h / 2
        tx, ty = rect["x"] + w / 2 + jx, rect["y"] + h / 2 + jy
        # Approach from a short offset: 2 intermediate moves, not a jump.
        ox, oy = random.uniform(-30, 30), random.uniform(-25, 25)
        self.move(tx + ox, ty + oy, tab=tab)
        time.sleep(random.uniform(0.03, 0.09))
        self.move(tx + ox * 0.4, ty + oy * 0.4, tab=tab)
        time.sleep(random.uniform(0.03, 0.08))
        self.move(tx, ty, tab=tab)
        time.sleep(random.uniform(0.05, 0.12))
        self.click(tx, ty, tab=tab)
        return (tx, ty)

    def click_element(self, selector, tab: int = 0, timeout=10.0):
        """Click the first element matching a CSS selector with a real
        pointer event (down+up at coordinates, so hover/focus behave),
        humanized: jittered target point, small approach moves and pauses.
        Returns (x, y); raises TimeoutError if the element never appears."""
        deadline = time.time() + timeout
        while True:
            rect = self.element_rect(selector, tab, timeout=2.0)
            if rect:
                return self._humanized_click(rect, tab=tab)
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
        returns the rect (see element_rect) or raises TimeoutError.

        Wedge-safe by design: never scrolls (no forced layouts) and keeps
        AT MOST ONE eval outstanding. A new poll is only sent after the
        previous one answered, so a slow or wedged page never accumulates
        a backlog of queued evals that would keep its script thread busy
        long after this call has given up (the Oracle SPA wedge).
        """
        deadline = time.time() + timeout
        js = ("(function(){var s=" + self._js_selector(selector) + ";"
              "var e=document.querySelector(s);if(!e){return null;}"
              "var r=e.getBoundingClientRect();"
              "return {x:r.x,y:r.y,w:r.width,h:r.height};})()")
        outstanding = None
        while True:
            if outstanding is None:
                req = self._alloc_id()
                self.cmd({"cmd": "eval", "tab": tab, "js": js, "id": req})
                outstanding = req
            try:
                ev = self.wait_for(
                    lambda e: (e.get("ev") == "evalresult"
                                and e.get("id") == outstanding),
                    timeout=1.0)
                outstanding = None  # answered: safe to poll again
                rect = ev.get("value")
                if isinstance(rect, dict) and "__value" in rect:
                    rect = rect["__value"]
                if rect and rect.get("w", 0) > 0 and rect.get("h", 0) > 0:
                    return rect
            except TimeoutError:
                # The eval is still outstanding on a slow page. Do NOT send
                # another one; just re-check the overall deadline.
                pass
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
        req = self._alloc_id()
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
        the document-start user script keeps in the hidden window.__browserr_int holder.
        Runs in every frame; this reads the top frame's ring."""
        js = ("JSON.stringify(((window.__browserr_int||{}).net||[]).slice(-%d))" % limit)
        raw = self.eval_js(js, tab, timeout)
        if isinstance(raw, str):
            return json.loads(raw)
        return raw or []

    def document_status(self, tab: int = 0, timeout=15.0):
        """Re-fetch the current document (same-origin HEAD) and return its
        HTTP status: distinguishes a served page (200/40x) from a network
        error (None on failure). The probe result also lands in the net
        ring as 'document-probe'. This exists because servo 0.8 never
        surfaces the main document's response status to page JS or the
        embedder - a block that serves a fake 404 looks identical to a
        real 404 without this. Returns (status, content_type) or
        (None, None) when the probe cannot run (non-http page, network
        error, timeout)."""
        marker = len(self.net_evidence(limit=500, tab=tab))
        r = self.eval_js(
            "(window.__browserr_int&&window.__browserr_int.probe)"
            "&&window.__browserr_int.probe()", tab)
        if r != "probe-started":
            return (None, None)
        deadline = time.time() + timeout
        while time.time() < deadline:
            ring = self.net_evidence(limit=500, tab=tab)
            for e in ring[marker:]:
                if e.get("kind") == "document-probe":
                    return (e.get("status"), e.get("type"))
                if e.get("kind") == "document-probe-error":
                    return (None, None)
            time.sleep(0.2)
        return (None, None)

    def frames(self, tab: int = 0, timeout=10.0):
        """The page's frame tree: {url, origin, children:[...]}.
        Same-origin frames are walked (title, nested children); cross-origin
        frames report their url only (servo 0.8 has no cross-frame JS)."""
        req = self._alloc_id()
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
        req = self._alloc_id()
        args = {"cmd": "download", "tab": 0, "id": req, "url": url, "path": path}
        if max_bytes:
            args["max_bytes"] = max_bytes
        self.cmd(args)
        ev = self.wait_for(
            lambda e: e.get("ev") == "download" and e.get("id") == req, timeout)
        if not ev.get("ok"):
            raise RuntimeError(f"download failed: {ev.get('error')}")
        return ev.get("bytes")

    def download_session(self, url, path, tab: int = 0, timeout=120.0,
                         max_bytes=None):
        """Download WITH the page's session (cookies): the fetch runs inside
        the tab, so logged-in portals work (invoices, CSV exports). Same-origin
        targets need no CORS; cross-origin ones need the server to allow the
        tab's origin. The engine's plain download() stays cookieless - this is
        the opt-in. Large files are base64'd through the eval channel, so keep
        max_bytes sane (default 64 MiB). Returns the byte count."""
        limit = int(max_bytes) if max_bytes else (64 << 20)
        js = ("(async () => { let r; try { r = await fetch(" +
              json.dumps(url) + ", {credentials: 'include'}); } "
              "catch (e) { return {error: String(e)}; } "
              "if (!r.ok) return {error: 'HTTP ' + r.status}; "
              "const buf = await r.arrayBuffer(); "
              "if (buf.byteLength > " + str(limit) + ") "
              "return {error: 'too large', size: buf.byteLength}; "
              "const b = new Uint8Array(buf); let bin = ''; "
              "for (let i = 0; i < b.length; i += 0x8000) "
              "bin += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000)); "
              "return {b64: btoa(bin)}; })()")
        res = self.eval_js(js, tab, timeout)
        if not isinstance(res, dict) or "b64" not in res:
            raise RuntimeError(f"session download failed: {res}")
        data = base64.b64decode(res["b64"])
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return len(data)

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
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass  # the OS-level guard (job handle) still reaps it
        if self._log:
            self._log.close()

    # -- SDK helpers (page extraction for agents) ---------------------------

    # Page scripts live in driver_js.py (pure data; kept out of the
    # transport). A class-body import binds them as class attributes,
    # so self.READ_JS and Driver.OUTCOME_JS keep working unchanged.
    from driver_js import (READ_JS, LINKS_JS, FORMS_JS, STRUCTURE_JS,
                           REF_RECT_JS, SET_FIELD_JS, FILL_FORM_JS,
                           SUBMIT_FORM_JS, OUTCOME_JS, HANDOFF_JS)

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
        # Humanized like click_element: jittered point, approach moves,
        # small pauses. Hover comes free from the moves before down/up.
        # _humanized_click wants a TOP-LEFT rect; REF_RECT_JS reports the
        # element CENTER, so normalize before jittering (otherwise the
        # click point lands around center + w/2, past the right edge).
        # The resolved rect is returned (unchanged API shape).
        if "w" in pt and "h" in pt:
            rect = {"x": pt["x"] - pt["w"] / 2,
                    "y": pt["y"] - pt["h"] / 2,
                    "w": pt["w"], "h": pt["h"]}
            self._humanized_click(rect, tab=tab)
        else:  # older rect shape without size: plain hover+click
            self.move(pt["x"], pt["y"], tab=tab)
            self.click(pt["x"], pt["y"], tab=tab)
        self._log_action("click_ref", ref=ref)
        return pt
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
        js = ("(() => { const r = (window.__browserr_int || {}).net || []; "
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
        # Cross-origin GET navigations take longer than the old page stays
        # idle, so the idle check must not win the race: it only counts
        # after a short grace period (same-origin navs land well inside it).
        idle_ok_after = time.time() + 1.0
        while time.time() < deadline:
            time.sleep(0.2)
            try:
                now = self.eval_js("location.href", tab, timeout=2.0)
            except TimeoutError:
                continue
            if now and now != before:
                navigated = True
                break
            if time.time() >= idle_ok_after and self.wait_network_idle(
                    quiet_ms=quiet_ms, timeout=0.5, tab=tab):
                break
        self.wait_network_idle(quiet_ms=quiet_ms, timeout=3.0, tab=tab)
        outcome = self.read_outcome(tab)
        out = {"submit": res, "navigated": navigated, "outcome": outcome}
        if not navigated and res.get("ok"):
            # servo 0.8 blocks page-initiated navigations to a MORE-LOCAL
            # address space (public->local, IP->localhost-name): a form GET
            # to such a target silently does nothing. Embedder-level loads
            # (navigate) are NOT blocked, so tell the agent the way out.
            try:
                action = res.get("action") or ""
                if action and self._host_of(action) not in ("", self._host_of(outcome.get("url") or "")) \
                        and "127.0.0.1" in (outcome.get("url") or ""):
                    out["nav_note"] = (
                        "page-initiated navigation may be blocked by "
                        "servo's local-network rule; verify with "
                        "navigate(action) which bypasses it")
            except (TypeError, ValueError):
                pass
        if evidence and self.evidence_dir:
            try:
                out["evidence"] = self.evidence(mode="plain", tab=tab)
            except (TimeoutError, EngineDeadError):
                out["evidence"] = {"error": "screenshot failed"}
        return out


    def detect_handoff(self, tab: int = 0, timeout=10.0):
        """Scan the page for things a human must do (CAPTCHA, OTP, password
        without a stored credential, payment fields, cookie consent).
        Returns {handoff, reasons, paused_hint}."""
        return self.eval_js(self.HANDOFF_JS, tab, timeout)

    # -- trust layer: evidence, action log, credentials, confirm gate -------

    @staticmethod
    def _host_of(url: str) -> str:
        """host[:port] of a URL; empty string when unparseable. Userinfo
        (user:pass@host) is stripped: servo puts answered credentials in
        the re-challenge URL's userinfo."""
        m = re.match(r"^[a-z][a-z0-9+.-]*://([^/?#]+)", (url or "").lower())
        if not m:
            return ""
        return m.group(1).rsplit("@", 1)[-1]

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
        """Append one JSON line to the action log (best effort; never raises).

        URL values are logged with their query string and fragment stripped:
        query strings carry session-ish tokens that must not outlive the
        call in an append-only file.
        """
        if not self.action_log_path:
            return
        try:
            line = {"t": round(time.time(), 3), "kind": kind}
            for key, value in fields.items():
                if isinstance(value, str) and (value.startswith("http://") or value.startswith("https://")):
                    value = value.split("?", 1)[0].split("#", 1)[0]
                line[key] = value
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
        with self._cond:
            self._auth_rejected.pop(host, None)

    def start_auto_auth(self) -> None:
        """Answer engine auth challenges from stored credentials, in the
        background. Unknown hosts fail closed (challenge dropped = no
        credentials sent). Idempotent.

        Rejection detection: a repeat challenge for the same host with the
        same credentials means the server REJECTED the previous answer
        (servo re-challenges on a 401). The stored credential is dropped
        and the new challenge fails closed, so a wrong credential cannot
        loop. The agent should call set_credentials with corrected values.
        """
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
                if creds is not None:
                    with self._cond:
                        rejected_at = self._auth_rejected.get(host)
                    if rejected_at is not None \
                            and rejected_at[0] == creds \
                            and time.time() - rejected_at[1] < 20.0:
                        # The same credentials were just rejected for this
                        # host: do not re-send. Drop the stored value and
                        # fail closed so a wrong credential cannot loop.
                        self._credentials.pop(host, None)
                        self._log_action("auth_rejected", host=host)
                        creds = None
                self.auth_answer(rid, creds[0] if creds else None,
                                 creds[1] if creds else None)
                if creds is not None:
                    with self._cond:
                        self._auth_rejected[host] = (creds, time.time())
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
                seen = self.eval_js("(window.__browserr_int||{}).submit_seen", tab, timeout=1.0)
            except TimeoutError:
                break
            if seen:
                break
            time.sleep(0.05)
        return res

    # -- net evidence / frames ------------------------------------------------

    def net_evidence(self, limit=200, tab: int = 0, timeout=10.0):
        """Recent page network activity (fetch/XHR) from the ring buffer
        the document-start user script keeps in the hidden window.__browserr_int holder.
        Runs in every frame; this reads the top frame's ring."""
        js = ("JSON.stringify(((window.__browserr_int||{}).net||[]).slice(-%d))" % limit)
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
