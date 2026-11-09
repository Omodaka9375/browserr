"""Autofill verification: navigate to a data: URL with a login form, run the
chrome's fill script (replicated here), and check the password field got the
value via document.title.
"""
import json
import socket
import struct
import threading
import time
from urllib.parse import quote

MAGIC = 0x46524D31
EV_MAGIC = 0x45565431
stop = False
titles = []


def drain(s):
    global stop
    while not stop:
        try:
            hdr = b""
            while len(hdr) < 20:
                s.settimeout(0.5)
                chunk = s.recv(20 - len(hdr))
                if not chunk:
                    return
                hdr += chunk
            magic = struct.unpack("<I", hdr[:4])[0]
            a = struct.unpack("<I", hdr[12:16])[0]
            b = struct.unpack("<I", hdr[16:20])[0]
            if magic == MAGIC:
                n = a * b * 4
                got = 0
                while got < n:
                    chunk = s.recv(min(1 << 20, n - got))
                    if not chunk:
                        return
                    got += len(chunk)
            elif magic == EV_MAGIC:
                payload = b""
                while len(payload) < a:
                    chunk = s.recv(a - len(payload))
                    if not chunk:
                        return
                    payload += chunk
                e = json.loads(payload)
                if e.get("ev") == "title" and e.get("title"):
                    titles.append(e["title"])
        except socket.timeout:
            continue
        except OSError:
            return


s = socket.create_connection(("127.0.0.1", 7411), timeout=10)
threading.Thread(target=drain, args=(s,), daemon=True).start()


def cmd(o):
    s.sendall((json.dumps(o) + "\n").encode())


page = ("<form><input id=username name=username><input id=password "
        "type=password name=password></form>")
url = "data:text/html," + quote(page)
cmd({"cmd": "navigate", "url": url, "tab": 0})
time.sleep(4)

# This mirrors chrome/src/main.rs fill_js() exactly.
user, pwd = "testuser", "s3cret"
fill = (
    "(()=>{const u=%s,p=%s;const set=(i,v)=>{i.value=v;"
    "i.dispatchEvent(new Event('input',{bubbles:true}));"
    "i.dispatchEvent(new Event('change',{bubbles:true}));};"
    "const pw=document.querySelector('input[type=password]');if(pw)set(pw,p);"
    "const re=/(user|email|login|account)/i;"
    "for(const t of document.querySelectorAll('input')){"
    "if(t.type==='password')continue;"
    "const k=(t.name||'')+' '+(t.id||'')+' '+(t.type||'')+' '+(t.autocomplete||'');"
    "if((!t.type||t.type==='text'||t.type==='email')&&re.test(k)){set(t,u);break;}}})()"
    % (json.dumps(user), json.dumps(pwd))
)
report = 'document.title="FILLED:"+document.querySelector("#username").value+","+document.querySelector("#password").value;'
cmd({"cmd": "eval", "tab": 0, "js": fill})
time.sleep(1)
cmd({"cmd": "eval", "tab": 0, "js": report})
time.sleep(3)
stop = True
s.close()

print("titles seen:", titles)
assert any(t.startswith("FILLED:") and "testuser" in t and "s3cret" in t for t in titles), \
    f"fill failed: {titles}"
print("FILL OK: username and password set")
