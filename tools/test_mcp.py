"""MCP server test: drive the browserr MCP server over stdio like a client.

Covers initialize, tools/list, and a full headless session through the tools:
start, ping, navigate, read, links, forms, fill, click_text, screenshot, tabs,
stop. Uses newline-delimited JSON-RPC on stdin/stdout (the MCP stdio shape).
"""

import os as _os
TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
ROOT = _os.path.dirname(TOOLS_DIR)
import base64
import functools
import http.server
import json
import subprocess
import sys
import threading
import time

sys.path.insert(0, TOOLS_DIR)

PAGE = """<!doctype html><html><head><title>MCP Test Page</title></head><body>
<p>Agent content here.</p>
<a id=next href='/mcp-next.html'>Next page</a>
<form action='/search'><input name='q' placeholder='Query'></form>
<input id='q2' value=''><button id='go2' onclick="document.getElementById('q2').dataset.done='1'">Go2</button>
</body></html>"""
NEXT = """<!doctype html><html><head><title>MCP Next</title></head><body>
<p>Second page content.</p></body></html>"""

import os
with open(TOOLS_DIR + "/mcp-dl.bin", "wb") as f:
    f.write(os.urandom(4096))

handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=TOOLS_DIR)
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8130), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()


class AuthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        credentials = self.headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(b"agent:secret").decode()
        if credentials == expected:
            body = b"<html><body>AUTHOK</body></html>"
            self.send_response(200)
        else:
            body = b"<html><body>DENIED</body></html>"
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="mcp-test"')
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


authd = http.server.ThreadingHTTPServer(("127.0.0.1", 8128), AuthHandler)
threading.Thread(target=authd.serve_forever, daemon=True).start()
PICKER_PATH = TOOLS_DIR + "/mcp-picker.txt"
with open(PICKER_PATH, "w") as f:
    f.write("MCP PICKED 42")
with open(TOOLS_DIR + "/mcp-picker.html", "w") as f:
    f.write("""<!doctype html><html><body><input id=pick type=file>
<div id=out></div>
<script>document.getElementById('pick')
.addEventListener('change',function(){var f=this.files[0];if(!f)return;
var r=new FileReader();r.onload=function(){
document.getElementById('out').textContent=f.name+':'+r.result};
r.readAsText(f);});</script></body></html>""")
with open(TOOLS_DIR + "/mcp-test.html", "w") as f:
    f.write(PAGE)
with open(TOOLS_DIR + "/mcp-next.html", "w") as f:
    f.write(NEXT)

proc = subprocess.Popen(
    [sys.executable, TOOLS_DIR + "/browserr_mcp.py"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
    cwd=TOOLS_DIR)


def send(msg):
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def recv():
    line = proc.stdout.readline()
    assert line, "server closed stdout"
    return json.loads(line)


def native_windows(pid):
    """Visible top-level HWNDs owned by a process (Windows only)."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = []
    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    return found


def call(name, args=None):
    send({"jsonrpc": "2.0", "id": call.n, "method": "tools/call",
          "params": {"name": name, "arguments": args or {}}})
    call.n += 1
    r = recv()
    assert "result" in r, r
    content = r["result"]["content"]
    assert content[0]["type"] == "text"
    if r["result"].get("isError"):
        raise AssertionError(f"tool error: {content[0]['text']}")
    return json.loads(content[0]["text"])


call.n = 1

try:
    send({"jsonrpc": "2.0", "id": 0, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05"}})
    init = recv()
    assert init["result"]["serverInfo"]["name"] == "browserr", init
    print("initialize OK")

    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    # (notification: no response expected)

    send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = recv()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert "browser_navigate" in names and "browser_read" in names, names
    expected_new = {"browser_wait_auth", "browser_auth_answer",
                    "browser_wait_filepicker", "browser_files_answer",
                    "browser_action_log"}
    assert expected_new <= names, expected_new - names
    start_schema = next(t for t in tools if t["name"] == "browser_start")
    assert "profile" in start_schema["inputSchema"]["properties"]
    assert "headed" in start_schema["inputSchema"]["properties"]
    print(f"tools/list OK ({len(names)} tools)")

    started = call("browser_start", {"auto_auth": False})
    assert started["started"] is True and started["auto_auth"] is False
    assert os.path.isabs(started["evidence_dir"]), started
    assert started["action_log_path"] == os.path.join(
        started["evidence_dir"], "actions.jsonl"), started
    print("start OK")
    print("ping:", call("browser_ping"))

    r = call("browser_navigate", {"url": "http://127.0.0.1:8130/mcp-test.html"})
    assert r["url"].endswith("mcp-test.html"), r
    log = call("browser_action_log", {})
    assert log["path"].endswith("actions.jsonl"), log
    assert any(e.get("kind") == "navigate" for e in log["entries"]), log
    print("action_log OK:", len(log["entries"]), "entries")
    print("navigate OK")

    r = call("browser_read")
    assert r["title"] == "MCP Test Page", r
    assert "Agent content here" in r["text"], r
    print("read OK")

    r = call("browser_links")
    assert any("mcp-next.html" in l["href"] for l in r["links"]), r
    print("links OK")

    r = call("browser_forms")
    assert any(f["name"] == "q" or f.get("name") == "q"
               for fr in r["forms"] for f in fr["fields"]), r
    print("forms OK")

    assert call("browser_fill", {"field": "q", "value": "mcp rocks"})["filled"] == "q"
    print("fill OK")

    call("browser_click_text", {"text": "Next page"})
    deadline = time.time() + 10
    while time.time() < deadline:
        if call("browser_read")["title"] == "MCP Next":
            break
        time.sleep(0.5)
    r = call("browser_read")
    assert r["title"] == "MCP Next" and "Second page" in r["text"], r
    print("click_text OK")

    r = call("browser_screenshot")
    assert r["path"].endswith(".png"), r
    print("screenshot OK ->", r["path"])

    r = call("browser_tabs", {"action": "open"})
    assert "tab" in r, r
    print("tabs OK:", r)

    r = call("browser_console", {})
    assert "messages" in r, r
    print("console OK:", len(r["messages"]), "messages")

    r = call("browser_download", {"url": "http://127.0.0.1:8130/mcp-dl.bin",
                                  "path": TOOLS_DIR + "/mcp-dl.bin"})
    assert r["bytes"] == 4096, r
    assert os.path.getsize(TOOLS_DIR + "/mcp-dl.bin") == 4096
    print("download OK:", r["bytes"], "bytes")

    # Element tools: back on the first page (click_text navigated away).
    call("browser_navigate", {"url": "http://127.0.0.1:8130/mcp-test.html"})
    r = call("browser_wait_for", {"selector": "#go2"})
    assert r["found"] == "#go2", r
    r = call("browser_read_structure", {})
    assert any(b["text"] == "Go2" for b in r["buttons"]), r["buttons"]
    sel = [i["selector"] for i in r["inputs"] if i["id"] == "q2"]
    assert sel, r["inputs"]
    print("read_structure OK, q2 selector:", sel[0])

    r = call("browser_wait_for", {"selector": "#go2"})
    assert r["found"] == "#go2", r
    print("wait_for OK:", r["rect"])

    r = call("browser_set_value", {"selector": "#q2", "value": "mcp"})
    assert r["set"] == "#q2", r
    r = call("browser_click_element", {"selector": "#go2"})
    assert r["clicked"] == "#go2", r
    print("element tools OK")

    # Manual auth is returned by navigate instead of wedging the load.
    r = call("browser_navigate", {"url": "http://127.0.0.1:8128/private"})
    challenge = r.get("auth_required")
    assert challenge and "127.0.0.1:8128" in challenge["url"], r
    assert call("browser_auth_answer", {
        "id": challenge["id"], "user": "agent", "password": "secret"
    })["credentials_sent"] is True
    deadline = time.time() + 10
    while time.time() < deadline:
        if "AUTHOK" in call("browser_read")["text"]:
            break
        time.sleep(0.2)
    assert "AUTHOK" in call("browser_read")["text"]
    print("manual auth tools OK")

    # File selection is an explicit MCP event and answer, not SDK-only.
    call("browser_navigate", {"url": "http://127.0.0.1:8130/mcp-picker.html"})
    r = call("browser_read_structure")
    picker = next(i for i in r["inputs"] if i["id"] == "pick")
    call("browser_click_ref", {"ref": picker["ref"]})
    picker_event = call("browser_wait_filepicker", {"timeout_seconds": 10})
    assert picker_event["id"] >= 1, picker_event
    call("browser_files_answer", {"id": picker_event["id"],
                                  "paths": [PICKER_PATH]})
    deadline = time.time() + 10
    picked = ""
    while time.time() < deadline:
        picked = call("browser_read")["text"]
        if "mcp-picker.txt:MCP PICKED 42" in picked:
            break
        time.sleep(0.2)
    assert "mcp-picker.txt:MCP PICKED 42" in picked, picked
    evidence = call("browser_evidence", {"mode": "plain"})
    assert evidence["action_log_path"] == started["action_log_path"], evidence
    assert os.path.isfile(evidence["path"]), evidence
    print("file picker and evidence paths OK")

    call("browser_stop")
    print("stop OK")

    # MCP itself can create a visible native window (not just the SDK test).
    started = call("browser_start", {"headed": True})
    assert started["headed"] is True, started
    deadline = time.time() + 15
    hwnds = []
    while time.time() < deadline and not hwnds:
        hwnds = native_windows(started["engine_pid"])
        time.sleep(0.2)
    if os.name == "nt" and os.environ.get("CI") != "true":
        assert hwnds, "MCP engine has no visible HWND"
        print("MCP native HWND OK")
    call("browser_navigate", {"url": "http://127.0.0.1:8130/mcp-test.html"})
    assert call("browser_read")["title"] == "MCP Test Page"
    call("browser_stop")
    print("MCP headed start, navigate, stop OK")

    print("ALL OK")
finally:
    try:
        proc.stdin.close()
    except OSError:
        pass
    proc.kill()
