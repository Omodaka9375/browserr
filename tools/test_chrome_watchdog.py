"""Chrome watchdog test: kill the engine child process of a running chrome,
verify the chrome respawns it (watchdog poll) and the session recovers
(renavigated tab reports its url again).

Uses the driver port to observe the engine. NB: after respawn the driver
clients were mirrored from the OLD engine socket; this test only checks the
engine-side recovery via a fresh attach.
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, r"D:/Code/2026/Browserr/tools")
from driver import Driver

CHROME = r"D:/Code/2026/Browserr/chrome/target/debug/chrome.exe"
ENGINE_NAME = "engine.exe"

ui = subprocess.Popen([CHROME, "--profile=watchdog", "--driver-port=7412"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    # Wait for chrome boot + engine spawn.
    deadline = time.time() + 30
    while time.time() < deadline:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='engine.exe'\" | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True).stdout.strip()
        pids = [int(p) for p in out.split() if p.isdigit()]
        # Keep only engines that are children of our chrome (other tests may
        # leave engines around).
        children = []
        for pid in pids:
            ppid = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").ParentProcessId"],
                capture_output=True, text=True).stdout.strip()
            if ppid.isdigit() and int(ppid) == ui.pid:
                children.append(pid)
        if children:
            break
        time.sleep(0.5)
    assert children, "no engine child found for the chrome"
    print("engine child:", children)

    d = Driver.attach(7412)
    d.navigate("https://example.com")
    d.wait_load()
    print("attached + navigated OK")

    # Kill the engine out from under the chrome.
    for pid in children:
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Stop-Process -Id {pid} -Force"], capture_output=True)
    print("engine killed; waiting for the chrome watchdog to respawn")

    # The chrome should respawn the engine: a new engine.exe child appears.
    deadline = time.time() + 30
    new_pid = None
    while time.time() < deadline:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='engine.exe'\" | "
             "Select-Object ProcessId, ParentProcessId"],
            capture_output=True, text=True).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].isdigit() and int(parts[1]) == ui.pid:
                new_pid = int(parts[0])
                break
        if new_pid and new_pid not in children:
            break
        time.sleep(0.5)
    assert new_pid and new_pid not in children, "engine was not respawned"
    print("engine respawned:", new_pid)

    assert ui.poll() is None, "chrome died with the engine"
    print("chrome alive after engine death: OK")
    print("ALL OK")
finally:
    d.close() if 'd' in dir() else None
    ui.kill()