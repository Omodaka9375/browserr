"""MCP server test: drive the browserr MCP server over stdio like a client.

Covers initialize, tools/list, and a full headless session through the tools:
start, ping, navigate, read, links, forms, fill, click_text, screenshot, tabs,
stop. Uses newline-delimited JSON-RPC on stdin/stdout (the MCP stdio shape).
"""
import functools
import http.server
import json
import subprocess
import sys
import threading
import time

sys.path.insert(0, r"D:/Code/2026/Browserr/tools")

PAGE = """<!doctype html><html><head><title>MCP Test Page</title></head><body>
<p>Agent content here.</p>
<a id=next href='/mcp-next.html'>Next page</a>
<form action='/search'><input name='q' placeholder='Query'></form>
<input id='q2' value=''><button id='go2' onclick="document.getElementById('q2').dataset.done='1'">Go2</button>
</body></html>"""
NEXT = """<!doctype html><html><head><title>MCP Next</title></head><body>
<p>Second page content.</p></body></html>"""

import os
with open(r"D:/Code/2026/Browserr/tools/mcp-dl.bin", "wb") as f:
    f.write(os.urandom(4096))

handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=r"D:/Code/2026/Browserr/tools")
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8130), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
with open(r"D:/Code/2026/Browserr/tools/mcp-test.html", "w") as f:
    f.write(PAGE)
with open(r"D:/Code/2026/Browserr/tools/mcp-next.html", "w") as f:
    f.write(NEXT)

proc = subprocess.Popen(
    [sys.executable, r"D:/Code/2026/Browserr/tools/browserr_mcp.py"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
    cwd=r"D:/Code/2026/Browserr/tools")


def send(msg):
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def recv():
    line = proc.stdout.readline()
    assert line, "server closed stdout"
    return json.loads(line)


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
    print(f"tools/list OK ({len(names)} tools)")

    assert call("browser_start")["started"] is True
    print("start OK")
    print("ping:", call("browser_ping"))

    r = call("browser_navigate", {"url": "http://127.0.0.1:8130/mcp-test.html"})
    assert r["url"].endswith("mcp-test.html"), r
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
                                  "path": r"D:/Code/2026/Browserr/tools/mcp-dl.bin"})
    assert r["bytes"] == 4096, r
    assert os.path.getsize(r"D:/Code/2026/Browserr/tools/mcp-dl.bin") == 4096
    print("download OK:", r["bytes"], "bytes")

    # Element tools: back on the first page (click_text navigated away).
    call("browser_navigate", {"url": "http://127.0.0.1:8130/mcp-test.html"})
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

    call("browser_stop")
    print("stop OK")

    print("ALL OK")
finally:
    try:
        proc.stdin.close()
    except OSError:
        pass
    proc.kill()
