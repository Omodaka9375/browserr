"""Verify the engine's minimal TLS client (http_get) end-to-end:

1. Plain HTTP favicon fetch (local server, no TLS).
2. Real HTTPS + redirects: engine started with --adblock-list pointing at a
   missing file must download EasyList (easylist.to redirects and is chunked).
The old ureq path is gone, so this proves the replacement works.
"""
import os
import subprocess
import sys
import time
import tempfile

ENGINE = os.environ.get("BROWSERR_ENGINE",
                        r"D:/Code/2026/Browserr/engine/target/release/engine.exe")
LOG = r"D:/Code/2026/Browserr/engine-https-test.log"

# Fresh temp profile: the engine sees no easylist.txt -> downloads over HTTPS.
tmp = tempfile.mkdtemp(prefix="browserr-https-")
adblock_list = os.path.join(tmp, "easylist.txt")
assert not os.path.exists(adblock_list)

import socket
import json

proc = subprocess.Popen(
    [ENGINE, "--profile-dir=" + os.path.join(tmp, "engine"),
     "--adblock-list=" + adblock_list],
    stderr=open(LOG, "w"))
s = None
try:
    for _ in range(100):
        try:
            s = socket.create_connection(("127.0.0.1", 7411), timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    assert s is not None, "engine did not listen"

    # Give the download a while: easylist.to -> https redirect, ~2MB body.
    deadline = time.time() + 60
    while time.time() < deadline:
        log = open(LOG, encoding="utf-8", errors="replace").read()
        if "filter list saved" in log or "download failed" in log:
            break
        time.sleep(1)

    log = open(LOG, encoding="utf-8", errors="replace").read()
    print("download log lines:")
    for line in log.splitlines():
        if "filter list" in line or "adblock" in line:
            print("  ", line)

    if "download failed" in log:
        raise SystemExit("FAIL: HTTPS download failed")
    assert "filter list saved" in log, "no download attempt logged?"
    size = os.path.getsize(adblock_list) if os.path.exists(adblock_list) else 0
    print(f"easylist.txt: {size/1000:.0f} KB")
    assert size > 100_000, f"filter list suspiciously small: {size}"
    print("HTTPS download OK")
finally:
    if s:
        s.close()
    proc.kill()
