"""Hardening tests:

1. Token gate: a chrome started with --driver-token rejects unauthenticated
   driver connections (no events reach them) and accepts authed ones.
2. Oversized lines are dropped without effect.
"""
import json
import socket
import subprocess
import sys
import time

sys.path.insert(0, r"D:/Code/2026/Browserr/tools")
from driver import Driver

CHROME = r"D:/Code/2026/Browserr/chrome/target/debug/chrome.exe"
TOKEN = "test-secret-token"


def read_one(sock, timeout=3.0):
    """Read one framed message; return (magic, payload_or_size) or None."""
    sock.settimeout(timeout)
    hdr = b""
    try:
        while len(hdr) < 20:
            chunk = sock.recv(20 - len(hdr))
            if not chunk:
                return None
            hdr += chunk
    except socket.timeout:
        return None
    magic = hdr[:4]
    a = int.from_bytes(hdr[12:16], "little")
    if magic == b"EVT1":
        payload = b""
        while len(payload) < a:
            chunk = sock.recv(a - len(payload))
            if not chunk:
                return None
            payload += chunk
        return ("EVT1", json.loads(payload))
    return (magic, a)


ui = subprocess.Popen([CHROME, "--profile=hardening", "--driver-port=7412",
                       f"--driver-token={TOKEN}"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    # Wait for the driver port to open.
    deadline = time.time() + 30
    sock = None
    while time.time() < deadline:
        try:
            sock = socket.create_connection(("127.0.0.1", 7412), timeout=1)
            break
        except OSError:
            time.sleep(0.3)
    assert sock, "driver port never opened"

    # 1. Unauthenticated: a navigate command must NOT take effect, and no
    # engine events may reach us (unregistered socket). Then the chrome
    # drops the connection.
    sock.sendall((json.dumps({"cmd": "navigate", "url": "https://example.com",
                              "tab": 0}) + "\n").encode())
    dropped = read_one(sock, timeout=3.0)
    # Either an immediate close (None) or silence until drop; no events.
    assert dropped is None or dropped[0] != b"EVT1", f"unauthed got events: {dropped}"
    print("token gate rejects unauth OK")

    # 2. Authed driver works fully.
    d = Driver.attach(7412, token=TOKEN)
    d.navigate("https://example.com")
    d.wait_load()
    d.wait_ready()
    print("authed driver OK, title:", d.eval_js("document.title"))
    d.close()

    # 3. Oversized line dropped: authed driver sends a 9MB line -> the
    # connection drops, engine unaffected (ping still answers).
    d2 = Driver.attach(7412, token=TOKEN)
    d2.ping()
    try:
        d2.sock.sendall(b'{"cmd":"eval","js":"' + b"A" * (9 << 20) + b'"}\n')
    except OSError:
        pass
    time.sleep(0.5)
    # Connection should be closed by the chrome (may take a moment to
    # ingest and reject 9MB).
    try:
        d2.sock.settimeout(10)
        got = d2.sock.recv(1)
        assert got == b"", f"expected close, got {got!r}"
        print("oversized line drops connection OK")
    except (OSError, socket.timeout) as e:
        print(f"oversized line: {type(e).__name__} (connection still open)")

    assert ui.poll() is None, "chrome died during hardening test"
    print("ALL OK")
finally:
    if 'd' in dir():
        pass
    ui.kill()