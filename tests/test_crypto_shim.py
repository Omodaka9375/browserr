"""crypto.subtle stand-in: hashes, HMAC and PBKDF2 against the stdlib.

The crates.io servo build has no crypto.subtle, so the engine installs a
pure-JS stand-in. Two things broke real sites: SHA-384 and SHA-512 were
wrong for most inputs (the 64-bit core read message words as signed), and
there was no key import at all, so a single-page app that signs each API
request with PBKDF2 could not load its data. Every result here is checked
against hashlib / hmac.
"""
import hashlib
import hmac
import http.server
import json
import os
import sys
import threading

TOOLS = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(os.path.dirname(TOOLS), "tools")
sys.path.insert(0, TOOLS)
import browserr_mcp as m

PORT = 8198
PAGE = b"<html><head><title>subtle</title></head><body>subtle</body></html>"


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)


server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
threading.Thread(target=server.serve_forever, daemon=True).start()

m.call_tool("browser_start", {})
r = m.call_tool("browser_navigate",
                {"url": f"http://127.0.0.1:{PORT}/", "wait_ms": 8000})
assert r["settled"], r
d = m.STATE.d()

HEX = ("const hex=(b)=>Array.from(new Uint8Array(b.buffer||b,b.byteOffset||0,"
       "b.byteLength)).map((x)=>x.toString(16).padStart(2,'0')).join('');"
       "const bytes=(a)=>new Uint8Array(a);")


def run(body):
    """Evaluate an async body in the page; the page awaits it."""
    return d.eval_js("(async()=>{%s %s})()" % (HEX, body), timeout=30.0)


HASHES = {"SHA-1": "sha1", "SHA-256": "sha256", "SHA-384": "sha384",
          "SHA-512": "sha512"}

# 1) Digests over every length across two block boundaries.
for name, py in HASHES.items():
    lengths = list(range(0, 300, 7)) + [55, 56, 63, 64, 111, 112, 127, 128]
    got = run("const out={};for(const n of %s){const m=new Uint8Array(n);"
              "for(let i=0;i<n;i++)m[i]=(i*7+3)&255;"
              "out[n]=hex(await crypto.subtle.digest(%s,m));}return out;"
              % (json.dumps(lengths), json.dumps(name)))
    for n in lengths:
        msg = bytes((i * 7 + 3) & 255 for i in range(n))
        want = hashlib.new(py, msg).hexdigest()
        assert got[str(n)] == want, (name, n, got[str(n)], want)
print("digest SHA-1/256/384/512 across block boundaries OK")

# 2) PBKDF2 deriveBits from a raw key.
CASES = [(b"password", b"salt", 1, 160), (b"password", b"salt", 1004, 512),
         (b"x" * 300, b"NaCl", 37, 328), (b"", b"", 2, 8)]
for name, py in HASHES.items():
    for secret, salt, iterations, bits in CASES:
        got = run("const k=await crypto.subtle.importKey('raw',bytes(%s),"
                  "'PBKDF2',false,['deriveBits']);"
                  "return hex(await crypto.subtle.deriveBits({name:'PBKDF2',"
                  "salt:bytes(%s),iterations:%d,hash:%s},k,%d));"
                  % (json.dumps(list(secret)), json.dumps(list(salt)),
                     iterations, json.dumps(name), bits))
        want = hashlib.pbkdf2_hmac(py, secret, salt, iterations,
                                   bits // 8).hex()
        assert got == want, (name, iterations, bits, got, want)
print("PBKDF2 deriveBits OK")

# 3) HMAC sign and verify from a raw key.
for name, py in HASHES.items():
    for key, msg in [(b"key", b"The quick brown fox"), (b"k" * 200, b""),
                     (b"", b"x")]:
        got = run("const k=await crypto.subtle.importKey('raw',bytes(%s),"
                  "{name:'HMAC',hash:{name:%s}},false,['sign','verify']);"
                  "const s=await crypto.subtle.sign('HMAC',k,bytes(%s));"
                  "return [hex(s),await crypto.subtle.verify('HMAC',k,s,"
                  "bytes(%s)),await crypto.subtle.verify('HMAC',k,s,"
                  "bytes([1,2,3]))];"
                  % (json.dumps(list(key)), json.dumps(name),
                     json.dumps(list(msg)), json.dumps(list(msg))))
        want = hmac.new(key, msg, py).hexdigest()
        assert got == [want, True, False], (name, got, want)
print("HMAC sign / verify OK")

# 4) What the stand-in cannot do still rejects, it does not pretend.
got = run("const out=[];for(const p of ["
          "()=>crypto.subtle.importKey('raw',bytes([1,2]),{name:'AES-GCM'},"
          "false,['encrypt']),"
          "()=>crypto.subtle.importKey('jwk',{},'PBKDF2',false,['deriveBits']),"
          "()=>crypto.subtle.encrypt({name:'AES-GCM'},{},bytes([1]))]){"
          "try{await p();out.push('resolved');}catch(e){out.push(e.name);}}"
          "return out;")
assert got == ["NotSupportedError"] * 3, got
print("unsupported operations still reject OK")

m.call_tool("browser_stop", {})
print("ALL OK")
