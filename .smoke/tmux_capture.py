"""Run the TUI inside tmux inside a pty; capture what tmux sends to the
outer terminal. Then analyze: passthrough envelopes, image geometry, and
cursor writes outside the reserved viewport."""
import os, pty, re, select, subprocess, sys, time

ROOT = "/mnt/d/Code/2026/Browserr"
ENV = {**os.environ, "TERM": "xterm-256color", "BROWSERR_ENGINE":
       f"{ROOT}/engine/target/release/engine", "HOME": "/root"}

cmd = ["tmux", "-f", "/root/.tmux.conf", "new-session", "-x", "120", "-y", "29",
       "python3", f"{ROOT}/term/browserr_term.py",
       f"file://{ROOT}/term/native-fixture.html",
       "--fps", "5", "--cell", "10:20"]

master, slave = pty.openpty()
p = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, env=ENV)
os.close(slave)
buf = b""
end = time.time() + 14
while time.time() < end:
    r, _, _ = select.select([master], [], [], .3)
    if r:
        try:
            buf += os.read(master, 65536)
        except OSError:
            break
p.terminate()
try:
    p.wait(3)
except Exception:
    p.kill()

out = "/mnt/d/Code/2026/Browserr/.smoke/tmux_outer.bin"
with open(out, "wb") as f:
    f.write(buf)
print(f"captured {len(buf)} bytes")

# Analysis
pt = buf.count(b"\x1bPtmux;")
print("passthrough envelopes:", pt)
# Sixel images inside envelopes
imgs = re.findall(rb"\x1bPtmux;(.*?)\x1b\\", buf, re.S)
sixel_imgs = [i for i in imgs if b"P7;1;0q" in i]
print("sixel passthrough writes:", len(sixel_imgs))
# Raster attribute dims of first few
for i in sixel_imgs[:3]:
    m = re.search(rb'P7;1;0q"1;1;(\d+);(\d+)', i)
    if m:
        print("  image raster:", m.group(1).decode(), "x", m.group(2).decode())
# Cursor positioning inside envelopes: max row used
rows_used = [int(r) for r in re.findall(rb"\x1b\x1b\[(\d+);\d+H", buf)]
if rows_used:
    print("cursor rows inside envelopes: max", max(rows_used))
# Raw (non-enveloped) absolute positioning after setup
raw_pos = [int(r) for r in re.findall(rb"\x1b\[(\d+);\d+H", buf)]
if raw_pos:
    print("raw cursor rows: max", max(raw_pos), "count", len(raw_pos))
# Any raw sixel leaked (not enveloped)?
leaks = len(re.findall(rb"\x1bP7;1;0q", buf)) - pt
print("raw sixel heads minus envelopes:", leaks)
