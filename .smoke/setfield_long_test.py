"""Long-value browser_set_field test, model boundary excluded.

Speaks newline JSON-RPC to the MCP server over stdio exactly like a real
MCP client (Kai) does: initialize -> initialized -> browser_start ->
navigate to a local textarea fixture -> browser_set_field with a ~900-char
multi-paragraph value -> read the value back via browser_structure ->

PASS criteria:
  1. The value reported by set_field equals what was sent.
  2. The value stored in the page equals what was sent.
Also probes a 64KB value to find where (if anywhere) the chain clips.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOLS = os.path.join(ROOT, "tools")

# --- fixture: a plain textarea page ----------------------------------------
tmpd = tempfile.mkdtemp(prefix="setfield-fx-")
with open(os.path.join(tmpd, "page.html"), "w", encoding="utf-8") as f:
    f.write("<!doctype html><html><body>"
            "<textarea name='text' rows='12' cols='70'></textarea>"
            "</body></html>")
os.chdir(tmpd)  # SimpleHTTPRequestHandler serves from CWD
httpd = HTTPServer(("127.0.0.1", 0), SimpleHTTPRequestHandler)
port = httpd.server_address[1]
import threading
threading.Thread(target=httpd.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{port}/page.html"

# The exact kind of text that failed on HN: multi-paragraph, newlines,
# bullets, a URL - ~900 chars.
LONG = (
    "Update: 0.7.0 is out.\n\n"
    "The big additions since this post:\n\n"
    "- A terminal client: the browser renders inside your terminal with Sixel\n"
    "  or Kitty graphics (half-block fallback), including over tmux. Clicks\n"
    "  and scrolling work; it degrades to text cells when the terminal has\n"
    "  no graphics protocol.\n"
    "- Native frame protocol: the engine encodes PNG/Sixel patches in Rust\n"
    "  and streams dirty rectangles, so a frame update is tens of KB instead\n"
    "  of raw pixels over the wire.\n"
    "- MCP named sessions: each session is its own engine with its own\n"
    "  profile and user agent, so you can run isolated browsing contexts\n"
    "  side by side.\n"
    "- An in-place updater shipped with the archives (update.ps1/update.sh):\n"
    "  it verifies the sha256 digest GitHub reports before replacing\n"
    "  anything and keeps a backup outside the install tree.\n"
    "- llms.txt at the site root for agents.\n\n"
    "https://github.com/Omodaka9375/browserr/releases/tag/v0.7.0\n"
)

BIG = ("0123456789abcdef" * 4096)  # 64KB, no newlines: raw size probe

# --- MCP client over stdio --------------------------------------------------
proc = subprocess.Popen(
    [sys.executable, os.path.join(TOOLS, "browserr_mcp.py")],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
    cwd=TOOLS, env={**os.environ, "BROWSERR_ENGINE": os.environ.get(
        "BROWSERR_ENGINE", os.path.join(ROOT, "engine", "target", "debug", "engine.exe"))},
)


def send(obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def recv_until_id(want, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("MCP server closed stdout")
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue  # server banner / noise
        if msg.get("id") == want:
            return msg
    raise TimeoutError(f"no response for id {want}")


def call(name, args=None, timeout=90):
    global _id
    _id += 1
    send({"jsonrpc": "2.0", "id": _id, "method": "tools/call",
          "params": {"name": name, "arguments": args}})
    r = recv_until_id(_id, timeout)
    result = r.get("result", {})
    if result.get("isError"):
        raise RuntimeError(f"{name} isError: " + content_text(result))
    return result


_id = 0
send({"jsonrpc": "2.0", "id": 0, "method": "initialize",
      "params": {"protocolVersion": "2024-11-05",
                 "capabilities": {}, "clientInfo": {"name": "setfield-long-test", "version": "0"}}})
init = recv_until_id(0)
print("initialize ok:", init["result"]["serverInfo"])
send({"jsonrpc": "2.0", "method": "notifications/initialized"})

call("browser_start", {"headed": False})
print("browser_start ok (default session, like the real client)")
call("browser_navigate", {"url": url})
print("navigate ok")

# 1) the HN-sized value
def content_text(result):
    """MCP tool result content -> concatenated text (handles content-item shapes)."""
    out = []
    for c in result["content"]:
        if isinstance(c, dict):
            out.append(c.get("text", json.dumps(c)))
        else:
            out.append(str(c))
    return "\n".join(out)


res = call("browser_set_field", {"target": "textarea[name=\"text\"]", "value": LONG})
reported = json.loads(content_text(res))
print(f"1) HN-sized value: sent={len(LONG)} chars, tool reported={len(reported['value'])} chars")
assert reported["ok"] is True
assert reported["value"] == LONG, "tool-reported value differs from what was sent"
print("   tool-reported value matches exactly")

struct = call("browser_read_structure")
d = json.loads(content_text(struct))
inp = next(i for i in d.get("inputs", []) if i.get("name") == "text")
print(f"   structure reports: value={len(inp['value'])} chars, value_clipped={inp.get('value_clipped')}")
assert inp["value"] == LONG[:200], "preview should be the first 200 chars"
assert inp.get("value_clipped") == len(LONG), "value_clipped must carry the true length"
print("   PASS: structure is an honest 200-char preview + true length")

# 3) 64KB probe
res = call("browser_set_field", {"target": "textarea[name=\"text\"]", "value": BIG})
reported = json.loads(content_text(res))
print(f"3) 64KB probe: sent={len(BIG)}, tool reported={len(reported['value'])}")
assert reported["value"] == BIG, "64KB value clipped by set_field"
print("   PASS: 64KB set_field identical")
struct = call("browser_read_structure")
d = json.loads(content_text(struct))
inp = next(i for i in d.get("inputs", []) if i.get("name") == "text")
assert inp.get("value_clipped") == len(BIG), "structure must report clipped 64KB length"
print(f"   PASS: structure reports value_clipped={inp['value_clipped']} for 64KB")

call("browser_stop")
proc.stdin.close()
proc.wait(timeout=10)
httpd.shutdown()
print("ALL OK - no truncation anywhere in the MCP chain")
