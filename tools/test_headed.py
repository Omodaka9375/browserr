"""Native window smoke test: a real HWND, native click/text, pause and tabs."""

import os as _os
TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
ROOT = _os.path.dirname(TOOLS_DIR)
import functools
import http.server
import sys
import threading
import time

sys.path.insert(0, TOOLS_DIR)
from driver import Driver, PausedError

if _os.name != "nt":
    raise SystemExit("test_headed currently validates Windows native windows")
import ctypes
ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

PAGE = """<!doctype html><html><head><title>Native Handoff</title></head><body>
<h1>Native window test</h1>
<input id=box autocomplete=off>
<button id=go>Continue</button>
<div id=out>waiting</div>
<script>
window.clicks = 0;
window.focuses = 0;
window.keylog = [];
document.addEventListener('keydown', function(e){window.lastKey=e.key});
window.moves = [];
document.addEventListener('pointermove', function(e){window.moves.push([e.clientX,e.clientY])});
document.getElementById('box').addEventListener('focus', function(){window.focuses++});
document.getElementById('box').addEventListener('keydown', function(e){window.keylog.push([e.key,e.code])});
document.getElementById('go').addEventListener('click', function(){
  window.clicks++;
  document.getElementById('out').textContent = 'clicked:' + window.clicks;
});
</script>
</body></html>"""
with open(TOOLS_DIR + "/headed-test.html", "w") as f:
    f.write(PAGE)
handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=TOOLS_DIR)
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8141), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def window_handles(pid):
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    handles = []
    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(hwnd, _):
        found = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(found))
        if found.value == pid and user32.IsWindowVisible(hwnd):
            handles.append(hwnd)
        return True

    assert user32.EnumWindows(visit, 0)
    return handles


def user32_title(hwnd):
    import ctypes
    title = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetWindowTextW(hwnd, title, len(title))
    return title.value


def move_mouse(x, y):
    """Inject one DPI-aware absolute pointer move with SendInput."""
    from ctypes import wintypes
    user32 = ctypes.windll.user32

    class MouseInput(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("extra", ctypes.POINTER(ctypes.c_ulong))]

    class InputUnion(ctypes.Union):
        _fields_ = [("mi", MouseInput)]

    class Input(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("data", InputUnion)]

    screen_w = user32.GetSystemMetrics(78)
    screen_h = user32.GetSystemMetrics(79)
    nx = int((x - user32.GetSystemMetrics(76)) * 65535 / screen_w)
    ny = int((y - user32.GetSystemMetrics(77)) * 65535 / screen_h)
    event = Input(type=0, data=InputUnion(mi=MouseInput(
        nx, ny, 0, 0x0001 | 0x8000 | 0x4000, 0, None)))
    assert user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) == 1
    time.sleep(0.2)
    actual = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(actual))
    assert abs(actual.x - x) <= 1 and abs(actual.y - y) <= 1, (
        (actual.x, actual.y), (x, y))


def poll(fn, timeout=10):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(0.1)
    raise AssertionError(f"timed out; last={last!r}")


d = Driver(ephemeral=True, engine_args=["--headed"],
           log=ROOT + "/engine-headed-test.log")
try:
    if _os.environ.get("CI") == "true":
        print("SKIP: native OS input test needs an interactive desktop")
        d.close()
        raise SystemExit(0)
    assert poll(lambda: window_handles(d.proc.pid)), "no visible HWND"
    hwnd = next(h for h in window_handles(d.proc.pid)
                if user32_title(h) == "Browserr")
    print("visible native HWND OK:", hwnd)
    d.navigate("http://127.0.0.1:8141/headed-test.html")
    d.wait_load(contains="headed-test.html")
    d.wait_ready()
    assert d.read_page()[0] == "Native Handoff"

    # Use the window handle to send actual OS input, not the driver protocol.
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)
    # Windows can refuse SetForegroundWindow from a background process, and
    # another window may overlap the target: OS input would land on it
    # instead. Force focus by clicking the engine window's own caption (the
    # title bar never runs page JS), then verify it is foreground.
    if user32.GetForegroundWindow() != hwnd:
        cap = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(cap))
        move_mouse((cap.left + cap.right) // 2, cap.top + 8)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.05)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(0.3)
    assert poll(lambda: user32.GetForegroundWindow() == hwnd), \
        "engine window never became the foreground window"
    rect = d.element_rect("#go")
    dpr = d.eval_js("devicePixelRatio") or 1
    point = wintypes.POINT(int((rect["x"] + rect["w"] / 2) * dpr),
                           int((rect["y"] + rect["h"] / 2) * dpr))
    assert user32.ClientToScreen(hwnd, ctypes.byref(point))
    move_mouse(point.x, point.y)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    assert d.eval_js("window.moves")
    assert poll(lambda: d.eval_js("window.clicks") == 1)
    print("native OS mouse click OK")

    # Pause forbids MCP input but does not disable the native window.
    d.pause("native handoff test")
    d.set_engine_paused(True)
    try:
        d.click(1, 1)
    except PausedError:
        pass
    else:
        raise AssertionError("driver click was not paused")
    try:
        d.activate(9)
    except PausedError:
        pass
    else:
        raise AssertionError("tab activation bypassed pause")
    d.sock.sendall(b'{"cmd":"activate","tab":9}\n')  # engine blocks raw writes too
    assert d.ping()["active"] == 0
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    assert poll(lambda: d.eval_js("window.clicks") == 2)
    print("human input works while driver paused OK")

    rect = d.element_rect("#box")
    point = wintypes.POINT(int((rect["x"] + rect["w"] / 2) * dpr),
                           int((rect["y"] + rect["h"] / 2) * dpr))
    user32.ClientToScreen(hwnd, ctypes.byref(point))
    move_mouse(point.x, point.y)
    assert d.eval_js("devicePixelRatio") >= 1.0
    assert d.eval_js("window.moves")
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    # Native key events from the focused OS window, not driver.type_text().
    for ch in "hi":
        user32.keybd_event(ord(ch.upper()), 0, 0, 0)
        time.sleep(0.05)
        user32.keybd_event(ord(ch.upper()), 0, 2, 0)
        time.sleep(0.1)
    assert poll(lambda: d.eval_js("window.focuses") == 1)
    print("native input focus OK")
    assert d.eval_js("document.activeElement.id") == "box"
    assert d.eval_js("window.keylog.length") == 2
    assert poll(lambda: d.eval_js("document.getElementById('box').value") == "hi")
    print("native OS keyboard text OK")
    user32.keybd_event(0x0D, 0, 0, 0)
    time.sleep(0.1)
    user32.keybd_event(0x0D, 0, 2, 0)
    assert poll(d.handoff_complete)
    print("native Enter explicitly completes handoff OK")
    d.resume()
    d.set_engine_paused(False)

    # The same native surface supports driver screenshots and tab switching.
    w, h, bgra = d.screenshot()
    assert w > 0 and len(bgra) == w * h * 4
    if dpr != 1:
        annotated = d.screenshot_annotated(TOOLS_DIR + "/headed-shot.png")
        assert annotated["pixels_drawn"] > 0
        assert annotated["items"], "scaled annotation has no targets"
    d.new_tab(1)
    d.activate(1)
    d.navigate("http://127.0.0.1:8141/headed-test.html", tab=1)
    d.wait_load(tab=1, contains="headed-test.html")
    d.wait_ready(tab=1)
    d.screenshot(tab=0)
    assert d.ping()["active"] == 1
    d.activate(0)
    print("native screenshots and tab visibility OK")

    print("ALL OK")
finally:
    d.close()
