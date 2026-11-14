"""Custom --user-agent: engine flag reaches pages (MCP spawn path)."""
import os
import sys
import time

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
from driver import Driver

CUSTOM = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "BrowserrSmoke/1.0 (+https://browserr.dev)")

d = Driver(ephemeral=True, engine_args=["--user-agent=" + CUSTOM])
d.navigate("https://httpbin.org/user-agent")
d.wait_load()
d.wait_ready()
time.sleep(0.5)
seen = d.eval_js("document.body.innerText")
print("reported:", seen)
assert "BrowserrSmoke/1.0" in str(seen), seen

# navigator.userAgent must agree (pages fingerprint via JS too).
nav = d.eval_js("navigator.userAgent")
assert "BrowserrSmoke/1.0" in str(nav), nav
print("navigator.userAgent agrees")

print("ALL OK")
d.close()
