"""Deterministic MCP handoff and request-cursor regression tests (no engine)."""

import os as _os
import sys
import time

TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
sys.path.insert(0, TOOLS_DIR)
import browserr_mcp
from driver import Driver


class FakeDriver:
    def __init__(self, detections, urls=None, paused="human takeover"):
        self.detections = iter(detections)
        self.urls = iter(urls or [])
        self.paused = paused
        self.url = "http://example.test/gate"
        self._handoff_seen = False

    def eval_js(self, _js, timeout=2.0):
        try:
            self.url = next(self.urls)
        except StopIteration:
            pass
        return self.url

    def handoff_complete(self, timeout=2.0):
        return False

    def detect_handoff(self, timeout=2.0):
        try:
            return next(self.detections)
        except StopIteration:
            return {"handoff": False, "reasons": []}


def wait_with(fake, timeout=0.01):
    old = browserr_mcp.STATE
    old_time = browserr_mcp.time.time
    old_sleep = browserr_mcp.time.sleep
    browserr_mcp.STATE = type("FakeState", (), {"d": lambda self: fake})()
    # Advance one virtual polling interval without depending on CPU speed.
    clock = [old_time()]
    browserr_mcp.time.time = lambda: clock[0]
    browserr_mcp.time.sleep = lambda seconds: clock.__setitem__(
        0, clock[0] + seconds)
    try:
        return browserr_mcp._wait_for_user(timeout_seconds=timeout)
    finally:
        browserr_mcp.STATE = old
        browserr_mcp.time.sleep = old_sleep
        browserr_mcp.time.time = old_time


try:
    # A clean first scan is not a completed handoff.
    result = wait_with(FakeDriver([{"handoff": False, "reasons": []}]))
    assert result["done"] is False and result["why"] == "timeout", result
    assert result["seen_handoff"] is False, result
    print("clean first scan waits OK")

    # Only an observed true-to-false transition completes by clearing.
    result = wait_with(FakeDriver([
        {"handoff": True, "reasons": [{"kind": "captcha"}]},
        {"handoff": False, "reasons": []},
    ]))
    assert result["done"] is True and result["why"] == "handoff cleared", result
    print("observed handoff clear OK")

    # A baseline read failure is not a navigation to the unchanged page.
    result = wait_with(FakeDriver(
        [{"handoff": False, "reasons": []}], urls=[TimeoutError()]))
    assert result["done"] is False and result["why"] == "timeout", result
    # A clear between calls still counts, without requiring a new true scan.
    fake = FakeDriver([{"handoff": True, "reasons": [{"kind": "captcha"}]},
                       {"handoff": True, "reasons": [{"kind": "captcha"}]}])
    result = wait_with(fake, timeout=0.3)
    assert result["done"] is False and fake._handoff_seen is True, result
    result = wait_with(fake, timeout=0.3)
    assert result["done"] is True and result["why"] == "handoff cleared", result
    print("read errors and cross-poll handoff clear OK")

    # A generic, undetected gate can still complete through navigation.
    result = wait_with(FakeDriver(
        [{"handoff": False, "reasons": []}],
        urls=["http://example.test/gate", "http://example.test/done"],
    ))
    assert result["done"] is True and result["why"] == "navigated", result
    print("generic gate navigation OK")

    # Native Enter confirms arbitrary challenges, even without detection.
    fake = FakeDriver([])
    fake.handoff_complete = lambda timeout=2.0: True
    result = wait_with(fake)
    assert result["done"] is True and result["why"] == "confirmed", result
    print("explicit native completion OK")

    # Waiting without an explicit handoff is a caller error, not success.
    try:
        wait_with(FakeDriver([], paused=None))
    except browserr_mcp.McpError as exc:
        assert "browser_pause" in str(exc), exc
    else:
        raise AssertionError("unpaused wait should fail")
    print("pause required OK")

    # Answered decision events are not replayed to later waiters.
    d = Driver.__new__(Driver)
    d._init_common(7411)
    d.events = [{"ev": "auth", "id": 41, "_driver_seq": 1}]
    d._event_seq = 1
    d._answered_auth.add(41)
    try:
        d.wait_auth(timeout=0.01)
    except TimeoutError:
        pass
    else:
        raise AssertionError("answered auth event was replayed")
    d.events.append({"ev": "auth", "id": 42, "_driver_seq": 2})
    assert d.wait_auth(timeout=0.01)["id"] == 42

    # Event cursors exclude history, including for a repeated URL.
    d.events.append({"ev": "url", "url": "http://same/", "_driver_seq": 3})
    d._event_seq = 3
    cursor = d.event_cursor()
    try:
        d.wait_for(lambda e: e.get("ev") == "url", timeout=0.01,
                   after=cursor)
    except TimeoutError:
        pass
    else:
        raise AssertionError("historical URL matched a fresh-event wait")
    print("decision replay and cursor guards OK")

    print("ALL OK")
finally:
    # Keep this test independent of elapsed scheduling around the short waits.
    time.sleep(0)
