"""Verify the engine's bind retry more rigorously: A holds the port, B retries,
A exits via chrome-disconnect, then B must bind and PRINT 'listening'. We check
B's log for its own listening line, not just a connect (which could hit A's
listen backlog)."""
import subprocess
import time

ENGINE = r"D:/Code/2026/Browserr/engine/target/debug/engine.exe"
ROOT = r"D:/Code/2026/Browserr"

a = subprocess.Popen([ENGINE], stderr=open(rf"{ROOT}/engine-bind-a.log", "wb"))
time.sleep(1.5)
b = subprocess.Popen([ENGINE], stderr=open(rf"{ROOT}/engine-bind-b.log", "wb"))
time.sleep(1.0)
print("A alive:", a.poll() is None, "| B alive:", b.poll() is None)

# A accepts the first connection; connect+close makes A exit and free the port.
import socket
s = socket.create_connection(("127.0.0.1", 7411), timeout=5)
s.close()
time.sleep(3)
print("A alive after close:", a.poll() is None)

ok = False
deadline = time.time() + 15
while time.time() < deadline:
    log = open(rf"{ROOT}/engine-bind-b.log").read()
    if "listening on 7411" in log:
        ok = True
        break
    time.sleep(0.5)

a.kill()
b.kill()
print("B bound the port after A exited:", ok)
for name in ("engine-bind-a.log", "engine-bind-b.log"):
    lines = open(rf"{ROOT}/{name}").read().strip().splitlines()
    print(name, "->", lines[-4:] if lines else "(empty)")
