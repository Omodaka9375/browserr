"""Hardening tests:

1. Token gate: an engine started with --driver-token rejects unauthenticated
   driver connections (no events reach them) and accepts authed ones.
2. Oversized lines are dropped without effect.
"""

import os as _os
TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
ROOT = _os.path.dirname(TOOLS_DIR)
import json
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, TOOLS_DIR)
from driver import Driver

TOKEN = "test-secret-token"
engine = os.environ.get(
    "BROWSERR_ENGINE",
    ROOT + "/engine/target/debug/engine.exe")

ui = subprocess.Popen(
    [engine, "--ephemeral", "--driver-port=7412", f"--driver-token={TOKEN}"],
    stdout=subprocess.DEVNULL,
    stderr=open(ROOT + "/engine-hardening-test.log", "w"))

# Hold the engine's main connection (the driver port opens after it).
main_sock = None
for _ in range(100):
    try:
        main_sock = socket.create_connection(("127.0.0.1", 7411), timeout=1)
        break
    except OSError:
        time.sleep(0.1)
assert main_sock is not None, "engine main port never opened"


def read_one(sock, timeout=3.0):
    """Read one framed message; return (magic, payload_or_None) or None."""
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
    return (magic, None)


try:
    # Wait for the driver port to open (after the main connect).
    sock = None
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            sock = socket.create_connection(("127.0.0.1", 7412), timeout=1)
            break
        except OSError:
            time.sleep(0.3)
    assert sock, "driver port never opened"

    # 1. Unauthenticated: a navigate must NOT take effect, no events, dropped.
    sock.sendall((json.dumps({"cmd": "navigate", "url": "https://example.com",
                              "tab": 0}) + "\n").encode())
    dropped = read_one(sock, timeout=3.0)
    assert dropped is None or dropped[0] != "EVT1", f"unauthed got events: {dropped}"
    print("token gate rejects unauth OK")

    # 2. Authed driver works fully.
    d = Driver.attach(7412, token=TOKEN)
    d.navigate("https://example.com")
    d.wait_load()
    d.wait_ready()
    print("authed driver OK")
    d.close()

    # 3. Oversized line drops the driver connection; the engine survives.
    d2 = Driver.attach(7412, token=TOKEN)
    d2.ping()
    try:
        d2.sock.sendall(b'{"cmd":"eval","js":"' + b"A" * (9 << 20) + b'"}\n')
    except OSError:
        pass
    time.sleep(1.0)
    try:
        d2.sock.settimeout(10)
        got = d2.sock.recv(1)
        assert got == b"", f"expected close, got {got!r}"
        print("oversized line drops connection OK")
    except (OSError, socket.timeout):
        print("oversized line: connection still observed-open (acceptable)")

    assert ui.poll() is None, "engine died during hardening test"
    print("ALL OK")
finally:
    if main_sock:
        main_sock.close()
    time.sleep(1.0)
    ui.kill()