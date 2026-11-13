"""Browserr MCP server: exposes the agent browser to MCP clients over stdio.

JSON-RPC 2.0 per the Model Context Protocol; stdlib only (json/socket/thread).
Manages one engine per session (spawns its own engine, ephemeral profile by
default; or attaches to a running engine).

Run:
    python browserr_mcp.py [--port 7412]   # or no args to spawn an engine

With --port it attaches to a running engine started with --driver-port=N
(computer-use mode); without it, it spawns its own engine.

Tools:
    browser_start / browser_stop / browser_ping
    browser_navigate / browser_read / browser_links / browser_forms
    browser_click / browser_click_text / browser_fill / browser_key
    browser_screenshot / browser_tabs
    browser_intercept / browser_pending_requests / browser_intercept_answer
    browser_net / browser_frames
    browser_set_field / browser_fill_form / browser_submit_form
    browser_read_outcome / browser_click_ref / browser_screenshot_annotated
    browser_pause / browser_resume / browser_detect_handoff
    browser_wait_for_user / browser_wait_auth / browser_auth_answer
    browser_wait_filepicker / browser_files_answer
    browser_evidence / browser_action_log / browser_set_credentials
    (profile on browser_start)
"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from driver import Driver, save_png


class McpError(Exception):
    def __init__(self, message):
        super().__init__(message)


class State:
    """One shared Driver instance; lazily started, stopped on session end."""

    def __init__(self):
        self.driver = None
        self.lock = threading.Lock()
        self.tab_counter = 1
        self.shot_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "mcp-shots")

    def d(self):
        with self.lock:
            if self.driver is None:
                raise McpError("browser not started; call browser_start first")
            return self.driver

    def status(self, **fields):
        """Session result with discoverable, absolute audit locations."""
        result = dict(fields)
        if self.driver is not None and self.driver.evidence_dir:
            result["evidence_dir"] = os.path.abspath(self.driver.evidence_dir)
            result["action_log_path"] = os.path.abspath(
                self.driver.action_log_path)
        return result

    @staticmethod
    def _engine_log_path():
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "mcp-engine.log")

    def start(self, attach_port=None, token=None, profile=None, auto_auth=True,
              headed=False):
        with self.lock:
            if self.driver is not None:
                return self.status(started=False, note="already running")
            if attach_port and headed:
                raise McpError("headed cannot change an attached engine; "
                               "start that engine with --headed instead")
            if attach_port:
                self.driver = Driver.attach(attach_port, token=token)
            elif profile:
                engine_args = ["--headed"] if headed else None
                # Persistent profile: logins and storage survive sessions.
                # Name is sanitized to a single path component.
                safe = "".join(c for c in profile if c.isalnum() or c in "-_")
                if not safe:
                    raise McpError("invalid profile name")
                pdir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "mcp-profiles", safe)
                os.makedirs(pdir, exist_ok=True)
                self.driver = Driver(profile_dir=pdir, engine_args=engine_args,
                                     log=self._engine_log_path())
            else:
                self.driver = Driver(ephemeral=True,
                                     engine_args=["--headed"] if headed else None,
                                     log=self._engine_log_path())
            # Trust layer on by default: evidence + action log per profile
            # (or per session for ephemeral/attach).
            label = profile or f"session-{time.time_ns()}"
            evdir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "mcp-evidence", safe_name(label))
            os.makedirs(evdir, exist_ok=True)
            self.driver.enable_evidence(evdir)
            if auto_auth:
                self.driver.start_auto_auth()
            # spawn = a fresh engine this server owns; attach = connected to
            # a running engine's driver port.
            return self.status(
                started=True, mode="attach" if attach_port else "spawn",
                engine_pid=self.driver.proc.pid if self.driver.proc else None,
                profile=profile or "ephemeral", auto_auth=bool(auto_auth),
                headed=bool(headed) if not attach_port else "unknown")

    def stop(self):
        with self.lock:
            if self.driver is None:
                return {"stopped": False, "note": "not running"}
            self.driver.close()
            self.driver = None
            return {"stopped": True}


def safe_name(s):
    return "".join(c for c in s if c.isalnum() or c in "-_") or "default"


STATE = State()
TOOLS = [
    {
        "name": "browser_start",
        "description": "Start the browser: spawns a fresh engine (ephemeral "
                       "profile) by default; pass attach_port to attach to a "
                       "running engine started with --driver-port. Pass token "
                       "if that engine was started with --driver-token. Set "
                       "auto_auth false to handle auth challenges manually. "
                       "headed true opens a real interactive native window; "
                       "it requires a desktop and graphics support. The "
                       "default remains headless CPU rendering.",
        "inputSchema": {"type": "object", "properties": {
            "attach_port": {"type": "integer",
                            "description": "driver port of a running engine"},
            "token": {"type": "string",
                      "description": "driver auth token if the engine was "
                                      "started with --driver-token"},
            "profile": {"type": "string",
                        "description": "persistent profile name (letters, digits, - and _)"},
            "auto_auth": {"type": "boolean",
                          "description": "answer auth from stored credentials; default true"},
            "headed": {"type": "boolean",
                       "description": "show a real interactive browser window (spawn only)"}},
            "required": []},
    },
    {
        "name": "browser_stop",
        "description": "Stop the browser (detaches in computer-use mode without "
                       "stopping the engine).",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_ping",
        "description": "Health check: returns the tab count and active tab id.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_navigate",
        "description": "Navigate the active tab to a URL and wait until the page "
                       "is ready for interaction.",
        "inputSchema": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]},
    },
    {
        "name": "browser_read",
        "description": "Extract the page as token-cheap text: title plus body "
                       "text with links as [label](href).",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_links",
        "description": "List visible links with labels, hrefs and viewport "
                       "rectangles for click targeting.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_forms",
        "description": "List visible forms and their fields (type, name, "
                       "placeholder, value, viewport rectangle).",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_click",
        "description": "Click page-relative coordinates (x, y).",
        "inputSchema": {"type": "object", "properties": {
            "x": {"type": "number"}, "y": {"type": "number"}},
            "required": ["x", "y"]},
    },
    {
        "name": "browser_click_text",
        "description": "Click the first visible link whose label contains the "
                       "given text.",
        "inputSchema": {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"]},
    },
    {
        "name": "browser_fill",
        "description": "Fill a form field by name or id and fire framework-safe "
                       "input/change events.",
        "inputSchema": {"type": "object", "properties": {
            "field": {"type": "string"}, "value": {"type": "string"}},
            "required": ["field", "value"]},
    },
    {
        "name": "browser_key",
        "description": "Press a key: an Enter/Backspace/... name or a single "
                       "character to type.",
        "inputSchema": {"type": "object", "properties": {
            "key": {"type": "string"}}, "required": ["key"]},
    },
    {
        "name": "browser_screenshot",
        "description": "Capture what the page shows and save it as a PNG; "
                       "returns the file path.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_click_element",
        "description": "Click the center of the first element matching a CSS "
                       "selector with a real pointer event (hover/focus "
                       "behave). Scrolls into view first.",
        "inputSchema": {"type": "object", "properties": {
            "selector": {"type": "string"},
            "timeout": {"type": "number"}}, "required": ["selector"]},
    },
    {
        "name": "browser_set_value",
        "description": "Set an input/textarea/select value by CSS selector, "
                       "firing input and change events (React/Vue controlled "
                       "inputs accepted via the native value setter).",
        "inputSchema": {"type": "object", "properties": {
            "selector": {"type": "string"},
            "value": {"type": "string"},
            "timeout": {"type": "number"}}, "required": ["selector", "value"]},
    },
    {
        "name": "browser_wait_for",
        "description": "Wait until a CSS selector matches at least one visible "
                       "element (SPA render); returns its viewport rect.",
        "inputSchema": {"type": "object", "properties": {
            "selector": {"type": "string"},
            "timeout": {"type": "number"}}, "required": ["selector"]},
    },
    {
        "name": "browser_read_structure",
        "description": "Structured page inventory (richer than read): headings, "
                       "buttons, inputs, selects, links, images - each with a "
                       "viewport rect and a usable CSS selector for "
                       "click_element/set_value. Prefer this when locating "
                       "specific UI; prefer read for prose.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_tabs",
        "description": "Open a new tab (action=open), switch tabs (action=activate "
                       "with tab id), or list tab ids (action=list).",
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["open", "activate", "list"]},
            "tab": {"type": "integer"}}, "required": ["action"]},
    },
    {
        "name": "browser_console",
        "description": "Page console messages collected so far, newest last: "
                       "{tab, level, message}. Network and TLS load errors also "
                       "arrive here. Optional level filter (log/info/warn/error).",
        "inputSchema": {"type": "object", "properties": {
            "level": {"type": "string"}}, "required": []},
    },
    {
        "name": "browser_download",
        "description": "Fetch a URL straight to a local path (no page cookies "
                       "are sent; use for public files). Returns the byte count.",
        "inputSchema": {"type": "object", "properties": {
            "url": {"type": "string"}, "path": {"type": "string"},
            "max_bytes": {"type": "integer"}}, "required": ["url", "path"]},
    },
    {
        "name": "browser_intercept",
        "description": "Arm or disarm fail-closed request interception. While "
                       "armed, qualifying requests (main-frame documents, "
                       "fetch/XHR, JSON) are HELD: the engine emits request "
                       "events and pages stall until browser_intercept_answer "
                       "decides them (allow/deny/respond) or the engine's 5s "
                       "fail-closed timeout denies. Use browser_pending_requests "
                       "to read held request ids.",
        "inputSchema": {"type": "object", "properties": {
            "on": {"type": "boolean"}}, "required": ["on"]},
    },
    {
        "name": "browser_pending_requests",
        "description": "Held requests awaiting decisions while interception "
                       "is armed: {id, tab, method, url, main} for each.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_intercept_answer",
        "description": "Decide a held request: action allow (pass through), "
                       "deny (synthetic 403), or respond (status/body/"
                       "content_type fulfilled by the engine).",
        "inputSchema": {"type": "object", "properties": {
            "id": {"type": "integer"},
            "action": {"type": "string",
                       "enum": ["allow", "deny", "respond"]},
            "status": {"type": "integer"},
            "body": {"type": "string"},
            "content_type": {"type": "string"}},
            "required": ["id", "action"]},
    },
    {
        "name": "browser_net",
        "description": "Recent page network activity (fetch/XHR ring, 200 "
                       "entries per frame, injected document-start so pages "
                       "cannot evade it): {kind, url, t, method?, status?} "
                       "newest last.",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer"}}, "required": []},
    },
    {
        "name": "browser_frames",
        "description": "The page's frame tree {url, origin, children:[...]}: "
                       "same-origin frames are walked (name, url, title); "
                       "cross-origin frames report their url only.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_set_field",
        "description": "Type-aware form field write. target is a stable ref "
                       "('e7') from browser_read_structure or a CSS selector. "
                       "Checkbox/radio take a boolean, <select> takes an "
                       "option value or its label, contenteditable takes "
                       "text. Fires input, change and blur so framework "
                       "bindings and validators run. Returns {ok, how, value}.",
        "inputSchema": {"type": "object", "properties": {
            "target": {"type": "string"},
            "value": {}}, "required": ["target", "value"]},
    },
    {
        "name": "browser_fill_form",
        "description": "Fill many fields in ONE round trip: fields maps a "
                       "stable ref or CSS selector to its value. Same "
                       "type-aware writes as browser_set_field. Returns "
                       "{ok, fields: {target: {ok, how, value}}}.",
        "inputSchema": {"type": "object", "properties": {
            "fields": {"type": "object",
                       "additionalProperties": {},
                       "description": "target -> value"}},
            "required": ["fields"]},
    },
    {
        "name": "browser_submit_form",
        "description": "Submit a form and report what happened. target "
                       "(optional) is a form, field or submit-button ref/"
                       "selector; without it the first form is used. "
                       "Dispatches the submit event (client-side validation "
                       "runs; preventDefault is honored), then submits "
                       "natively, waits for the navigation or network "
                       "quiet, and returns {submit, navigated, outcome}. "
                       "FAIL-CLOSED: if the page shows payment or captcha "
                       "conditions, the submit refuses to run unless a "
                       "human confirmed (confirm: true). evidence: true "
                       "saves a screenshot of the landed page.",
        "inputSchema": {"type": "object", "properties": {
            "target": {"type": "string"},
            "quiet_ms": {"type": "integer"},
            "confirm": {"type": "boolean",
                        "description": "human confirmation for payment/captcha pages"},
            "evidence": {"type": "boolean",
                         "description": "save a screenshot of the landed page"}},
            "required": []},
    },
    {
        "name": "browser_read_outcome",
        "description": "What the page says after an action: url, title, "
                       "alert/aria-live texts (validation errors, toasts) "
                       "and fields violating required/pattern.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_click_ref",
        "description": "Click the center of a stable ref from the last "
                       "browser_read_structure (hover first, then a real "
                       "pointer click). Fails cleanly on a stale ref.",
        "inputSchema": {"type": "object", "properties": {
            "ref": {"type": "string"}}, "required": ["ref"]},
    },
    {
        "name": "browser_screenshot_annotated",
        "description": "Screenshot with every interactive element boxed and "
                       "numbered by its stable ref (element 7 is ref e7), "
                       "plus the item list with labels, so an agent and a "
                       "human can both point at the same element. Returns "
                       "{path, items: [{ref, num, label, x, y, w, h}]}.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_pause",
        "description": "Hand control to the user. Every mutating action "
                       "(navigate, click, type, fill, submit) fails with a "
                       "clear 'paused' error until browser_resume; reads "
                       "(read_structure, read_page, screenshots, "
                       "read_outcome, detect_handoff) keep working so the "
                       "agent can watch the page and wait.",
        "inputSchema": {"type": "object", "properties": {
            "reason": {"type": "string"}}, "required": []},
    },
    {
        "name": "browser_resume",
        "description": "Take control back after a user handoff.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_detect_handoff",
        "description": "Scan the page for things a human must do: CAPTCHA "
                       "iframes, one-time-code inputs, empty password "
                       "fields, payment card fields, cookie-consent walls. "
                       "Returns {handoff, reasons: [{kind, detail}]}. Use "
                       "before pausing to tell the user WHY they are needed.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_evidence",
        "description": "Save a screenshot to the evidence directory as "
                       "proof of what the page looked like at this moment. "
                       "mode 'plain' saves the frame; 'annotated' adds "
                       "numbered boxes per element ref and returns the item "
                       "list. Returns {path, ...}.",
        "inputSchema": {"type": "object", "properties": {
            "mode": {"type": "string", "enum": ["plain", "annotated"]}},
            "required": []},
    },
    {
        "name": "browser_action_log",
        "description": "Read back the append-only action log (what the "
                       "engine did and when: navigates, fills, submits, "
                       "clicks, evidence, auth answers). Newest last. "
                       "Returns {path, entries}.",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000}},
            "required": []},
    },
    {
        "name": "browser_set_credentials",
        "description": "Store credentials for a host so HTTP auth "
                       "challenges for it are answered automatically; hosts "
                       "without stored credentials fail closed (challenge "
                       "denied, nothing sent). Credentials never appear in "
                       "the action log.",
        "inputSchema": {"type": "object", "properties": {
            "host": {"type": "string",
                     "description": "hostname or full URL (host[:port] kept)"},
            "user": {"type": "string"},
            "password": {"type": "string"}},
            "required": ["host", "user", "password"]},
    },
    {
        "name": "browser_wait_auth",
        "description": "Wait for an unanswered HTTP auth challenge and return "
                       "{id, tab, url, proxy}. The engine denies unanswered "
                       "challenges after 10 seconds. Start with auto_auth "
                       "false to handle challenges with browser_auth_answer.",
        "inputSchema": {"type": "object", "properties": {
            "tab": {"type": "integer"},
            "timeout_seconds": {"type": "number"}}, "required": []},
    },
    {
        "name": "browser_auth_answer",
        "description": "Answer an HTTP auth challenge. Omit user/password or "
                       "pass null to deny it without sending credentials.",
        "inputSchema": {"type": "object", "properties": {
            "id": {"type": "integer"}, "user": {"type": "string"},
            "password": {"type": "string"}}, "required": ["id"]},
    },
    {
        "name": "browser_wait_filepicker",
        "description": "Wait for an unanswered file picker and return "
                       "{id, tab, multiple}. The engine cancels unanswered "
                       "pickers after 60 seconds; answer with "
                       "browser_files_answer.",
        "inputSchema": {"type": "object", "properties": {
            "tab": {"type": "integer"},
            "timeout_seconds": {"type": "number"}}, "required": []},
    },
    {
        "name": "browser_files_answer",
        "description": "Choose local file paths for a pending file picker; "
                       "an empty or omitted paths list cancels it.",
        "inputSchema": {"type": "object", "properties": {
            "id": {"type": "integer"},
            "paths": {"type": "array", "items": {"type": "string"}}},
            "required": ["id"]},
    },
    {
        "name": "browser_wait_for_user",
        "description": "Long-poll while paused: returns when the page "
                       "navigates or a previously detected handoff condition "
                       "clears. An unchanged or unrecognized gate does NOT "
                       "count as completion. In a native window the user can "
                       "press Enter after a generic or cross-origin challenge "
                       "to confirm completion. Returns done:false on timeout; "
                       "call again to keep watching.",
        "inputSchema": {"type": "object", "properties": {
            "timeout_seconds": {"type": "number"}}, "required": []},
    },
]


def _wait_for_user(timeout_seconds=30):
    """Watch a paused page without treating a clean first scan as success.

    Completion requires navigation or a true-to-false handoff transition
    observed by this call. Generic gates are not detectable, so they keep
    waiting until the human navigates or the agent resumes explicitly.
    """
    d = STATE.d()
    if d.paused is None:
        raise McpError("call browser_pause before browser_wait_for_user")
    timeout = max(0.0, min(float(timeout_seconds), 120))
    start = time.time()
    # Remember a detected gate across polling calls for the same pause.
    seen_handoff = getattr(d, "_handoff_seen", False)
    last_handoff = {"handoff": False, "reasons": []}
    try:
        before_url = d.eval_js("location.href", timeout=2.0)
        if not isinstance(before_url, str):
            before_url = None
    except TimeoutError:
        before_url = None  # unknown is not a changed page
    while time.time() - start < timeout:
        try:
            now_url = d.eval_js("location.href", timeout=2.0)
            if (isinstance(now_url, str) and before_url is not None
                    and now_url != before_url):
                return {"done": True, "why": "navigated",
                        "url": now_url, "handoff": d.detect_handoff(timeout=2.0)}
            if isinstance(now_url, str):
                before_url = now_url
            handoff = d.detect_handoff(timeout=2.0)
            if isinstance(handoff, dict) and isinstance(
                    handoff.get("handoff"), bool):
                last_handoff = handoff
                detected = handoff["handoff"]
                if seen_handoff and not detected:
                    d._handoff_seen = False
                    return {"done": True, "why": "handoff cleared",
                            "handoff": last_handoff}
                seen_handoff = seen_handoff or detected
                d._handoff_seen = seen_handoff
        except TimeoutError:
            pass  # page mid-navigation; keep watching
        if d.handoff_complete(timeout=2.0):
            return {"done": True, "why": "confirmed",
                    "handoff": last_handoff}
        time.sleep(min(0.5, max(0.001, start + timeout - time.time())))
    return {"done": False, "why": "timeout",
            "seen_handoff": seen_handoff,
            "handoff": last_handoff,
            "hint": "Human input is unchanged or not recognized; "
                    "check the visible browser, then resume when done."}


def call_tool(name, args):
    if name == "browser_start":
        return STATE.start(args.get("attach_port"), token=args.get("token"),
                           profile=args.get("profile"),
                           auto_auth=args.get("auto_auth", True),
                           headed=args.get("headed", False))
    if name == "browser_stop":
        return STATE.stop()

    d = STATE.d()
    if name == "browser_ping":
        return d.ping()
    if name == "browser_navigate":
        cursor = d.event_cursor()
        d.navigate(args["url"])
        # Auth holds a main-frame load. Return that decision to the caller
        # instead of waiting for a page that cannot arrive until it is answered.
        target = args["url"].split("://", 1)[-1][:48] or None
        ev = d.wait_for(
            lambda e: ((e.get("ev") == "auth" and e.get("tab") == 0
                        and e.get("id") not in d._answered_auth)
                       or (e.get("ev") == "url" and e.get("tab") == 0
                           and (target is None or target in e.get("url", "")))),
            timeout=15.0, after=cursor)
        if ev.get("ev") == "auth":
            return {"url": ev.get("url"), "auth_required": {
                "id": ev.get("id"), "tab": ev.get("tab"),
                "url": ev.get("url"), "proxy": ev.get("proxy")}}
        d.wait_ready()
        return {"url": d.eval_js("location.href")}
    if name == "browser_read":
        title, text = d.read_page()
        return {"title": title, "text": text}
    if name == "browser_read_structure":
        return d.read_structure()
    if name == "browser_links":
        return {"links": d.links()}
    if name == "browser_forms":
        return {"forms": d.forms()}
    if name == "browser_click":
        d.click(args["x"], args["y"])
        return {"clicked": [args["x"], args["y"]]}
    if name == "browser_click_text":
        hit = d.click_text(args["text"])
        if hit is None:
            raise McpError(f"no visible link matching {args['text']!r}")
        return {"clicked": hit}
    if name == "browser_click_element":
        d.click_element(args["selector"], timeout=args.get("timeout", 10.0))
        return {"clicked": args["selector"]}
    if name == "browser_set_value":
        d.set_value(args["selector"], args["value"],
                    timeout=args.get("timeout", 10.0))
        return {"set": args["selector"]}
    if name == "browser_wait_for":
        rect = d.wait_for_selector(args["selector"],
                                    timeout=args.get("timeout", 15.0))
        return {"found": args["selector"], "rect": rect}
    if name == "browser_fill":
        if not d.fill(args["field"], args["value"]):
            raise McpError(f"no field named {args['field']!r}")
        return {"filled": args["field"]}
    if name == "browser_key":
        k = args["key"]
        if len(k) == 1:
            d.type_text(k)
        else:
            d.key(k)
            d.key(k, down=False)
        return {"key": k}
    if name == "browser_screenshot":
        w, h, bgra = d.screenshot()
        os.makedirs(STATE.shot_dir, exist_ok=True)
        path = os.path.join(STATE.shot_dir, f"shot-{int(__import__('time').time())}.png")
        save_png(path, w, h, bgra)
        return {"path": path, "w": w, "h": h}
    if name == "browser_tabs":
        action = args["action"]
        if action == "open":
            with STATE.lock:
                tid = STATE.tab_counter
                STATE.tab_counter += 1
            d.new_tab(tid)
            d.activate(tid)
            return {"tab": tid}
        if action == "activate":
            d.activate(args["tab"])
            return {"active": args["tab"]}
        return d.ping()
    if name == "browser_console":
        msgs = d.console(level=args.get("level"))
        return {"messages": msgs[-200:]}
    if name == "browser_download":
        n = d.download(args["url"], args["path"],
                       max_bytes=args.get("max_bytes"))
        return {"path": args["path"], "bytes": n}
    if name == "browser_intercept":
        d.intercept(args["on"])
        return {"intercept": args["on"]}
    if name == "browser_pending_requests":
        reqs = [e for e in d.events if e.get("ev") == "request"
                and e.get("id") not in d._answered_requests]
        return {"pending": [{"id": r["id"], "tab": r["tab"],
                              "method": r["method"], "url": r["url"],
                              "main": r["main"]} for r in reqs[-50:]]}
    if name == "browser_intercept_answer":
        d.request_answer(args["id"], args["action"],
                         status=args.get("status", 200),
                         body=args.get("body", ""),
                         content_type=args.get("content_type", "text/plain"))
        return {"answered": args["id"], "action": args["action"]}
    if name == "browser_net":
        return {"entries": d.net_evidence(limit=args.get("limit", 200))}
    if name == "browser_frames":
        return d.frames()
    if name == "browser_set_field":
        return d.set_field(args["target"], args["value"])
    if name == "browser_fill_form":
        return d.fill_form(args["fields"])
    if name == "browser_submit_form":
        return d.submit_and_wait(args.get("target"),
                                 quiet_ms=args.get("quiet_ms", 500),
                                 confirm=args.get("confirm", False),
                                 evidence=args.get("evidence", False))
    if name == "browser_evidence":
        return STATE.status(**d.evidence(mode=args.get("mode", "plain")))
    if name == "browser_action_log":
        return STATE.status(**d.action_log(limit=args.get("limit", 100)))
    if name == "browser_wait_auth":
        ev = d.wait_auth(tab=args.get("tab"),
                         timeout=args.get("timeout_seconds", 10.0))
        return {"id": ev.get("id"), "tab": ev.get("tab"),
                "url": ev.get("url"), "proxy": ev.get("proxy")}
    if name == "browser_auth_answer":
        user, password = args.get("user"), args.get("password")
        d.auth_answer(args["id"], user, password)
        return {"answered": args["id"], "credentials_sent": bool(
            user is not None and password is not None)}
    if name == "browser_wait_filepicker":
        ev = d.wait_filepicker(tab=args.get("tab"),
                               timeout=args.get("timeout_seconds", 60.0))
        return {"id": ev.get("id"), "tab": ev.get("tab"),
                "multiple": ev.get("multiple")}
    if name == "browser_files_answer":
        d.files_answer(args["id"], args.get("paths"))
        return {"answered": args["id"], "paths": args.get("paths") or []}
    if name == "browser_set_credentials":
        d.set_credentials(args["host"], args["user"], args["password"])
        return {"stored": True, "host": d._host_of(args["host"]) or args["host"]}
    if name == "browser_read_outcome":
        return d.read_outcome()
    if name == "browser_click_ref":
        return {"clicked": args["ref"], "at": d.click_ref(args["ref"])}
    if name == "browser_screenshot_annotated":
        os.makedirs(STATE.shot_dir, exist_ok=True)
        path = os.path.join(STATE.shot_dir,
                            f"annotated-{int(__import__('time').time())}.png")
        return d.screenshot_annotated(path)
    if name == "browser_pause":
        d.pause(args.get("reason") or "user takeover")
        d.set_engine_paused(True)
        d._handoff_seen = False
        return {"paused": True, "reason": d.paused,
                "completion": "Press Enter in the native window when done",
                "hint": "Interact directly in the native window when started "
                        "with headed:true. Without it, only screenshots are "
                        "available; cross-origin CAPTCHAs need headed mode."}
    if name == "browser_resume":
        d.resume()
        d.set_engine_paused(False)
        return {"paused": False}
    if name == "browser_detect_handoff":
        return d.detect_handoff()
    if name == "browser_wait_for_user":
        return _wait_for_user(args.get("timeout_seconds", 30))
    raise McpError(f"unknown tool {name}")


def handle(msg):
    """Return the JSON-RPC response dict for one request, or None (notify)."""
    method = msg.get("method")
    msg_id = msg.get("id")
    try:
        if method == "initialize":
            # Echo the client's requested protocol version when given (MCP
            # negotiation): clients validate the echoed version and drop
            # the connection on a mismatch.
            requested = (msg.get("params") or {}).get("protocolVersion")
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "protocolVersion": requested or "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "browserr", "version": "0.5.0"}
            }}
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
        if method == "tools/call":
            params = msg.get("params", {})
            result = call_tool(params.get("name"), params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text",
                              "text": json.dumps(result, ensure_ascii=False)}],
                "isError": False,
            }}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        if msg_id is not None:
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32601, "message": f"unknown method {method}"}}
        return None
    except McpError as e:
        if msg_id is None:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "content": [{"type": "text", "text": str(e)}], "isError": True,
        }}
    except Exception as e:  # noqa: BLE001 - surface everything to the client
        if msg_id is None:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "content": [{"type": "text",
                          "text": f"{type(e).__name__}: {e}"}],
            "isError": True,
        }}


def main():
    attach_port = None
    argv = sys.argv[1:]
    if "--port" in argv:
        attach_port = int(argv[argv.index("--port") + 1])
    STATE.attach_port = attach_port  # docs only; start decides
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(msg)
        if resp is not None:
            out.write(json.dumps(resp) + "\n")
            out.flush()
    # stdin closed: session over, stop the engine if we own it
    STATE.stop()


if __name__ == "__main__":
    main()
