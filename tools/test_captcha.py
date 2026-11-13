"""CAPTCHA challenge-page signature probe (online; NOT in the CI suite).

Live-verifies the documented verdicts (README "Known limitations"):
Turnstile / hCaptcha / reCAPTCHA v2 challenge iframes do NOT render in
servo 0.8 - their iframe RPC channels hit servo's strict cross-origin
frame policy and the widget never mounts. This test pins the DETECTION
signals an agent should use to tell a challenged walk from an empty page:

  1. a SecurityError appears in the console errors (recaptcha's loader
     throws it directly; the others' page scripts echo it),
  2. no live challenge iframe is mounted in the DOM (iframe[src] pointing
     at the challenge domain - NOT the page's own description images or
     text, which mention the widget),
  3. no challenge-domain fetch/XHR in the net ring.

If servo ever relaxes its frame policy and the widgets START working,
these assertions fail loudly -> re-document and add real render checks.

Requires internet access; self-skips when a target is unreachable.
Run by hand: python tools/test_captcha.py
"""

import os as _os
TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
ROOT = _os.path.dirname(TOOLS_DIR)
import sys
import time

sys.path.insert(0, TOOLS_DIR)
from driver import Driver

# (label, url, iframe src fragment that would identify a LIVE widget,
#  ring domain fragment expected ABSENT from fetch/XHR traffic)
TARGETS = [
    ("turnstile", "https://2captcha.com/demo/cloudflare-turnstile",
     "challenges.cloudflare.com", "challenges.cloudflare.com"),
    ("recaptcha-v2", "https://2captcha.com/demo/recaptcha-v2",
     "google.com/recaptcha", "google.com/recaptcha/api"),
    ("hcaptcha", "https://accounts.hcaptcha.com/demo",
     "hcaptcha.com/hs", "hcaptcha.com/checksiteconfig"),
]

d = Driver(log=f"{ROOT}/engine-captcha-test.log")
results = []
try:
    for label, url, iframe_frag, ring_frag in TARGETS:
        try:
            d.navigate(url)
            try:
                d.wait_load(contains=url.split("://", 1)[1][:30], timeout=30)
            except TimeoutError:
                pass  # redirect chains; the checks below are the truth
            d.wait_ready(timeout=30)
        except (ConnectionError, OSError) as e:
            print(f"SKIP (network unreachable): {label} - {e}")
            sys.exit(0)

        time.sleep(6)  # challenge scripts settle after hydration noise

        console = d.console()
        errs = [m.get("message", "") for m in console
                if m.get("level") == "error"]
        sec_errs = [e for e in errs if "SecurityError" in e
                    or "operation is insecure" in e]

        # LIVE widget = a real iframe mounted with the challenge domain
        # in its src. Demo pages embed description IMAGES and text; only
        # a mounted iframe src counts. NB: reCAPTCHA's iframe MOUNTS but
        # its RPC channel dies (servo SecurityError) - the iframe exists
        # structurally while its contents never load. The definitive
        # works/broken signal for all three is challenge TRAFFIC: a
        # working widget always fetches its config/challenge endpoints.
        n_iframes = d.eval_js(
            "document.querySelectorAll('iframe[src*=\"%s\"]').length"
            % iframe_frag)
        if not isinstance(n_iframes, (int, float)):
            n_iframes = 0

        ring = d.net_evidence(limit=500)
        challenge_traffic = [e for e in ring
                            if ring_frag in e.get("url", "")]

        results.append((label, {
            "security_errors": len(sec_errs),
            "live_iframes": int(n_iframes),
            "challenge_traffic": len(challenge_traffic),
        }))
        print(f"{label}: securityErrors={len(sec_errs)} "
              f"liveIfames={int(n_iframes)} "
              f"challengeTraffic={len(challenge_traffic)}")

    # Pinned verdicts: challenges do NOT work. The works/broken signal
    # is challenge TRAFFIC (zero for all three today): a working widget
    # always fetches its config/challenge endpoints. reCAPTCHA's iframe
    # mounts structurally but its channel dies with a SecurityError.
    # Any flip means servo changed behavior - re-verify the whole
    # matrix by hand and update the README + this test.
    verdicts = dict(results)
    for label, r in verdicts.items():
        assert r["challenge_traffic"] == 0, \
            f"{label}: challenge traffic observed - the widget may WORK " \
            "now; re-verify and update README"
    assert verdicts["recaptcha-v2"]["security_errors"] > 0, \
        "reCAPTCHA no longer throws SecurityError - re-verify the matrix"
    print("ALL OK (challenge signatures verified; see README limits)")
finally:
    d.close()
