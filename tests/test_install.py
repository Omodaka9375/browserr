"""Verify a packaged release archive the way an install actually behaves.

Unpacks nowhere special, runs the engine from the unpack (never from the
source tree), and drives it through the MCP server shipped in the archive -
the exact path a user takes after downloading the zip. Render-dependent.

    python tests/test_install.py <archive.zip|.tar.gz>
    python tests/test_install.py --auto     (picks the newest at repo root)
"""
import glob
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Files every archive must contain regardless of platform.
COMMON = ["engine", "driver.py", "driver_js.py", "annotate.py",
          "browserr_mcp.py", "README.md", "LICENSE"]
WINDOWS_ONLY = ["mcp_wrap.cmd", "browserr-term.cmd", "update.cmd", "update.ps1"]
UNIX_ONLY = ["browserr-term", "update.sh"]

# The tools added for 1.0.0; a stale archive is the failure this catches.
NEW_TOOLS = ["browser_scroll", "browser_resize", "browser_history"]


def pick_archive():
    found = []
    for pattern in ("browserr-*.zip", "browserr-*.tar.gz"):
        found += glob.glob(os.path.join(ROOT, pattern))
    if not found:
        raise SystemExit("no release archive at the repo root; run "
                         "scripts/package.ps1 or scripts/package.sh first")
    return max(found, key=os.path.getmtime)


def unpack(archive, dest):
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
            # zipfile drops the exec bit; Windows does not care, unix does.
            for info in z.infolist():
                if info.external_attr >> 28 == 0o3:
                    target = os.path.join(dest, info.filename)
                    if os.path.exists(target) and os.name != "nt":
                        mode = os.stat(target).st_mode | 0o111
                        os.chmod(target, mode)
    else:
        with tarfile.open(archive) as t:
            t.extractall(dest)


def main(argv):
    archive = argv[1] if len(argv) > 1 and argv[1] != "--auto" \
        else pick_archive()
    if not os.path.isabs(archive):
        archive = os.path.join(ROOT, archive)
    print(f"archive: {os.path.basename(archive)} "
          f"({os.path.getsize(archive) / (1 << 20):.1f} MB)")

    tmp = tempfile.mkdtemp(prefix="browserr-install-")
    unpack(archive, tmp)

    present = set(os.listdir(tmp))
    missing = [f for f in COMMON if f not in present]
    for f in COMMON:
        if f == "engine" and os.name == "nt" and "engine.exe" in present:
            missing = [m for m in missing if m != "engine"]
    if missing:
        raise SystemExit(f"archive is missing {missing}")
    print("payload OK:", len(present), "entries")
    plat = sorted(present & set(WINDOWS_ONLY + UNIX_ONLY))
    print("platform launchers:", plat)

    exe = "engine.exe" if os.name == "nt" else "engine"
    engine = os.path.join(tmp, exe)
    out = subprocess.run([engine, "--version"], capture_output=True, text=True,
                         timeout=60)
    reported = (out.stdout or "").strip()
    print("engine reports:", reported or f"<no output> rc={out.returncode}")
    if not reported.startswith("browserr "):
        raise SystemExit("engine --version did not answer")

    # Drive the SHIPPED MCP server, not the repo copy.
    mcp = os.path.join(tmp, "browserr_mcp.py")
    script = f"""
import json, sys
sys.path.insert(0, {tmp!r})
import browserr_mcp as m
tools = sorted(t["name"] for t in m.TOOLS)
print("JSON:" + json.dumps({{
    "count": len(tools),
    "new": [t for t in {NEW_TOOLS!r} if t in tools],
    "version": m.MCP_VERSION,
    "engine": m.ENGINE_VERSION,
    "engine_path": m.ENGINE_PATH,
}}))
"""
    env = dict(os.environ, BROWSERR_ENGINE=engine)
    probe = subprocess.run([sys.executable, "-c", script], env=env,
                           capture_output=True, text=True, timeout=120)
    line = next((ln for ln in probe.stdout.splitlines()
                 if ln.startswith("JSON:")), None)
    if not line:
        raise SystemExit(
            f"MCP server failed to load:\n{probe.stdout}\n{probe.stderr}")
    info = json.loads(line[5:])
    print(f"MCP server {info['version']} -> engine {info['engine']}")
    print(f"engine path: {info['engine_path']}")
    print(f"tools: {info['count']}")
    if info["count"] < 45:
        raise SystemExit(f"expected >= 45 tools, got {info['count']}")
    if len(info["new"]) != len(NEW_TOOLS):
        raise SystemExit(
            f"archive is stale: missing new tools "
            f"{set(NEW_TOOLS) - set(info['new'])}")
    print("new tools present:", ", ".join(info["new"]))

    if info["version"] != info["engine"]:
        raise SystemExit(
            f"version mismatch: MCP {info['version']} vs engine {info['engine']}")

    # One real session from the unpack: navigate, scroll, click by ref.
    session = f"""
import os, sys, threading, http.server, functools, time
sys.path.insert(0, {tmp!r})
import browserr_mcp as m

PAGE = (b"<html><head><title>InstallCheck</title></head>"
        b"<body style='height:2000px'>"
        b"<button id=b onclick=\\"document.title='CLICKED'\\">GO</button>"
        b"</body></html>")

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8213), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
m.call_tool("browser_start", {{}})
d = m.STATE.d()
r = m.call_tool("browser_navigate", {{"url": "http://127.0.0.1:8213/", "settle_ms": 8000}})
assert d.eval_js("document.title") == "InstallCheck", d.eval_js("document.title")
before = d.eval_js("window.scrollY")
m.call_tool("browser_scroll", {{"dy": 300}})
time.sleep(0.4)
after = d.eval_js("window.scrollY")
assert after > before, (before, after)
st = d.read_structure()
refs = [e.get("ref") for e in (st.get("buttons") or []) if e.get("ref")]
assert refs, st
m.call_tool("browser_click_ref", {{"ref": refs[0]}})
time.sleep(0.3)
assert d.eval_js("document.title") == "CLICKED", d.eval_js("document.title")
ev = m.call_tool("browser_evidence", {{"mode": "plain"}})
assert os.path.isfile(ev["path"]), ev
m.call_tool("browser_stop", {{}})
print("SESSION-OK")
"""
    sess = subprocess.run([sys.executable, "-c", session], env=env,
                          capture_output=True, text=True, timeout=300)
    if "SESSION-OK" not in sess.stdout:
        raise SystemExit(
            f"live session failed:\n{sess.stdout[-2000:]}\n{sess.stderr[-3000:]}")
    print("live session OK: navigate, scroll, click_ref, evidence")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print("ALL OK")


if __name__ == "__main__":
    main(sys.argv)
