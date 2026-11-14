"""Named sessions: two engines side by side, each with its own UA/proxy group.

Proxy routing itself is upstream-dependent, so the verifiable contract is:
distinct engines, distinct UAs, independent browsing per session name, and
the default session behavior unchanged (no session field).
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browserr_mcp as m

# Session A: custom UA.
r = m.call_tool("browser_start",
                {"session": "research", "user_agent": "SessionA-UA/1.0"})
assert r["started"] and r["session"] == "research", r

# Session B: different UA, runs CONCURRENTLY on its own engine port.
r = m.call_tool("browser_start",
                {"session": "shopping", "user_agent": "SessionB-UA/2.0"})
assert r["started"] and r["session"] == "shopping", r

# Both sessions browse independently and report their own UA.
for name, ua in (("research", "SessionA-UA/1.0"), ("shopping", "SessionB-UA/2.0")):
    m.call_tool("browser_navigate",
                {"url": "https://httpbin.org/user-agent",
                 "session": name, "settle_ms": 10000})
    text = m.call_tool("browser_read", {"session": name})["text"]
    assert ua in text, (name, text[:120])
    print(f"session {name}: UA verified")

# Distinct engines: pids must differ.
pa = m.STATE.d("research").proc.pid
pb = m.STATE.d("shopping").proc.pid
assert pa != pb, "sessions share one engine"
print("distinct engines:", pa, pb)

# Errors are session-scoped: an unknown session name fails loudly.
try:
    m.call_tool("browser_ping", {"session": "nope"})
    raise SystemExit("missing session did not raise")
except m.McpError as e:
    assert "not started" in str(e)
print("unknown session fails closed")

# Stop one session; the other keeps browsing.
m.call_tool("browser_stop", {"session": "research"})
try:
    m.call_tool("browser_ping", {"session": "research"})
    raise SystemExit("stopped session still reachable")
except m.McpError:
    pass
m.call_tool("browser_navigate",
            {"url": "https://example.com", "session": "shopping",
             "settle_ms": 10000})
title = m.call_tool("browser_read", {"session": "shopping"})["title"]
assert title == "Example Domain", title
print("remaining session still browsing")

m.call_tool("browser_stop", {"session": "shopping"})
print("ALL OK")
