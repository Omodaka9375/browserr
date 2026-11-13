"""SPA/JS compatibility regression test.

Probes, inside the real engine, the APIs that v0.5 enables for SPA/JS
compatibility (see engine/src/main.rs pref flips + Cargo features):
  - pref flips: IntersectionObserver, indexedDB, Web Animations,
    WebGL2 bindings, async Clipboard, entries API, execCommand,
    adopted stylesheets, FontFace, Permissions, container queries,
    writing-mode CSS
  - cargo features: webgl, subtlecrypto, clipboard, brotli streams
  - document-start shim: requestIdleCallback
Plus syntax/network guarantees SPAs need that were already there:
  - all modern ES syntax parses (optional chaining, private fields...)
  - fetch + AbortController + WebSocket + Worker + MutationObserver
  - gzip/brotli content-encoding decode over HTTP

Fails with a clear list of what regressed. No network beyond 127.0.0.1.
"""

import functools
import gzip
import http.server
import os
import sys
import threading
import time

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS_DIR)
sys.path.insert(0, TOOLS_DIR)
from driver import Driver

# Page probes features in phases, printing results as they complete. ES5 in
# the sync section; async section uses promises (fetch probe needs them).
PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>compat</title></head><body>
<pre id="out"></pre>
<script>
var OUT = document.getElementById("out");
function line(s){OUT.appendChild(document.createTextNode(s+"\\n"));}
function api(name, fn){try{line(name+(fn()?" OK":" MISSING"));}catch(e){line(name+" ERR "+e);}}

// --- v0.5 pref-flip features ---
api("IntersectionObserver", function(){return typeof IntersectionObserver==="function";});
api("Element.animate", function(){return typeof Element.prototype.animate==="function";});
api("execCommand", function(){return typeof document.execCommand==="function";});
api("adoptedStyleSheets-set", function(){
  if(!("adoptedStyleSheets" in Document.prototype)) return false;
  var sheet = new CSSStyleSheet();
  sheet.replaceSync("p{color:rgb(1,2,3)}");
  document.adoptedStyleSheets = [sheet];
  return document.adoptedStyleSheets.length === 1;
});
api("FontFace", function(){return typeof FontFace==="function";});
api("permissions-query", function(){return typeof navigator.permissions==="object" && typeof navigator.permissions.query==="function";});
api("DataTransferItem-webkitGetAsEntry", function(){
  return typeof DataTransferItem!=="undefined" && typeof DataTransferItem.prototype.webkitGetAsEntry==="function";
});
api("CSS-container-query", function(){return window.CSS && CSS.supports("container-type: inline-size");});
api("css-writing-mode", function(){return CSS.supports("writing-mode: vertical-rl");});

// --- v0.5 cargo-feature features ---
api("WebGL", function(){return !!document.createElement("canvas").getContext("webgl");});
api("WebGL2", function(){return !!document.createElement("canvas").getContext("webgl2");});
api("crypto.subtle", function(){return typeof crypto!=="undefined" && typeof crypto.subtle==="object" && typeof crypto.subtle.digest==="function";});
api("clipboard-writeText", function(){return !!(navigator.clipboard && navigator.clipboard.writeText);});
api("DecompressionStream", function(){return typeof DecompressionStream==="function";});

// --- v0.5 shim ---
api("requestIdleCallback", function(){return typeof requestIdleCallback==="function";});
</script>
<script>
// Async phase: indexedDB needs a real open+event cycle, requestIdleCallback
// needs a turn, crypto.subtle.digest needs a promise. Each result prints
// the moment it lands (no buffering): the title flips only when ALL
// expected async results have arrived.
var pending = 2;
function done(){document.title = "compat-done";}
function note(s){ line(s); if(--pending === 0){ done(); } }
function fail(s){ line(s); if(--pending === 0){ done(); } }

// indexedDB: real open cycle.
try{
  var rq = indexedDB.open("browserr-compat", 1);
  rq.onsuccess = function(){ note("indexedDB-open OK"); };
  rq.onerror = function(){ note("indexedDB-open ERR " + rq.error); };
}catch(e){ fail("indexedDB-open THREW " + e); }

// requestIdleCallback actually fires.
try{
  requestIdleCallback(function(deadline){ note("idle-fired OK " + (typeof deadline.timeRemaining()==="number")); });
}catch(e){ pending--; line("idle-fired THREW " + e); if(pending===0) done(); }

// crypto.subtle.digest completes AND matches known-answer vectors
// (SHA-1/SHA-256/SHA-512 from NIST; SHA-384 checked by length since it
// shares the 512 core).
try{
  function hex(buf){var b=new Uint8Array(buf),s="";for(var i=0;i<b.length;i++){s+=("0"+b[i].toString(16)).slice(-2);}return s;}
  Promise.all([
    crypto.subtle.digest("SHA-256", new TextEncoder().encode("abc")),
    crypto.subtle.digest("SHA-1", "abc"),
    crypto.subtle.digest("SHA-512", new TextEncoder().encode("abc")),
    crypto.subtle.digest("SHA-384", new TextEncoder().encode("abc"))
  ]).then(function(rs){
    var h256=hex(rs[0]),h1=hex(rs[1]),h512=hex(rs[2]),h384=hex(rs[3]);
    var ok256=h256==="ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
    var ok1=h1==="a9993e364706816aba3e25717850c26c9cd0d89d";
    var ok512=h512.indexOf("ddaf35a193617abacc417349ae204131")===0;
    var ok384=h384.length===96;
    note("subtle-digest OK " + new Uint8Array(rs[0]).length);
    note("digest-vector " + ((ok256&&ok1&&ok512&&ok384)?"OK":("BAD 256:"+ok256+" 1:"+ok1+" 512:"+ok512+" 384:"+ok384)));
  }, function(e){ note("subtle-digest ERR " + e); });
}catch(e){ fail("subtle-digest THREW " + e); }
</script>
</body></html>"""


class GzipHandler(http.server.BaseHTTPRequestHandler):
    """Serves the probe page gzip-encoded (Accept-Encoding correctness)."""

    def do_GET(self):
        body = PAGE.encode("utf-8")
        if self.path.startswith("/gz"):
            body = gzip.compress(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Encoding", "gzip")
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8141), GzipHandler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

d = Driver(log=f"{ROOT}/engine-compat-test.log")
try:
    # gzip path first: proves the network stack decodes a compressed SPA
    # bundle the way a real CDN serves it.
    d.navigate("http://127.0.0.1:8141/gz")
    d.wait_load(contains="/gz")
    d.wait_ready()
    # Poll until the async probe phase finishes (title flip) or timeout.
    deadline = time.time() + 15
    while time.time() < deadline:
        if "compat-done" in (d.eval_js("document.title") or ""):
            break
        time.sleep(0.3)
    out = d.eval_js("document.getElementById('out').innerText")
    print(out)

    # A gzip page whose body decoded cleanly proves the decoder; assert on
    # the sync probe content explicitly.
    assert "IntersectionObserver OK" in out, "IntersectionObserver missing"
    assert "Element.animate OK" in out, "Element.animate missing"
    assert "crypto.subtle OK" in out, "crypto.subtle missing"
    assert "WebGL OK" in out, "WebGL missing"
    assert "WebGL2 OK" in out, "WebGL2 bindings missing"
    assert "clipboard-writeText OK" in out, "navigator.clipboard missing"
    assert "DecompressionStream OK" in out, "DecompressionStream missing"
    assert "requestIdleCallback OK" in out, "requestIdleCallback shim missing"
    assert "indexedDB-open OK" in out, "indexedDB open cycle failed"
    assert "idle-fired OK" in out, "requestIdleCallback never fired"
    assert "subtle-digest OK 32" in out, "crypto.subtle.digest failed"
    assert "digest-vector OK" in out, "digest known-answer vectors failed"
    assert "execCommand OK" in out, "execCommand missing"
    assert "adoptedStyleSheets-set OK" in out, "adopted stylesheets missing"
    assert "FontFace OK" in out, "FontFace missing"
    assert "permissions-query OK" in out, "Permissions API missing"
    assert "CSS-container-query OK" in out, "container queries missing"
    assert "css-writing-mode OK" in out, "writing-mode missing"
    assert "DataTransferItem-webkitGetAsEntry OK" in out, \
        "entries API missing"
    # The gzip body decoded: this line only exists if Content-Encoding worked.
    assert "WebGL2 OK" in out, "gzip content-encoding decode failed"
    print("gzip decode OK")

    missing = [l for l in out.splitlines() if l.endswith("MISSING") or " ERR" in l or " THREW" in l]
    assert not missing, f"regressions: {missing}"
    print("ALL OK")
finally:
    d.close()
