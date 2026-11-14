"""MCP-level custom UA: browser_start(user_agent=...) -> navigate -> read."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browserr_mcp as m

r = m.call_tool("browser_start", {"user_agent": "SmokeUA/2.0 browserr-mcp"})
assert r.get("started"), r
m.call_tool("browser_navigate",
            {"url": "https://httpbin.org/user-agent", "settle_ms": 8000})
text = m.call_tool("browser_read", {})["text"]
assert "SmokeUA/2.0" in text, text[:200]
print("MCP user_agent verified:", [l for l in text.splitlines() if "Smoke" in l])
m.call_tool("browser_stop", {})
print("ALL OK")
