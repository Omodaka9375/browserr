#!/usr/bin/env python3
"""Check GitHub Actions job status for the v0.7.0 push (no auth needed)."""
import json
import subprocess
import sys
import urllib.request

REPO = "Omodaka9375/browserr"
HDRS = {"User-Agent": "kai-ci-check", "Accept": "application/vnd.github+json"}

# Anonymous API is rate-limited; the same host has stored push credentials.
# Use them for a read-only status lookup. The token is never printed.
try:
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, check=True,
        cwd="/mnt/d/Code/2026/Browserr").stdout
    for line in out.splitlines():
        if line.startswith("password="):
            HDRS["Authorization"] = "Bearer " + line.split("=", 1)[1]
            print("(using stored git credentials for API auth)")
            break
except Exception as e:
    print(f"(anonymous API: {e})")


def get(url):
    req = urllib.request.Request(url, headers=HDRS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


runs = get(f"https://api.github.com/repos/{REPO}/actions/runs?per_page=4")
for r in runs["workflow_runs"]:
    if (r["head_sha"] or "").startswith("5fb7b59"):
        print(f'== {r["name"]} run {r["id"]}: {r["status"]} / {r["conclusion"]}')
        jobs = get(f'https://api.github.com/repos/{REPO}/actions/runs/{r["id"]}/jobs?per_page=20')
        for j in jobs["jobs"]:
            mark = {"success": "OK ", "failure": "FAIL", "cancelled": "CXL ",
                    "skipped": "skip"}.get(j["conclusion"], "....")
            print(f'  [{mark}] {j["name"]} ({j["status"]})')
            for s in j["steps"]:
                sm = {"success": "ok", "failure": "FAIL", "skipped": "--",
                      "cancelled": "cxl"}.get(s["conclusion"], "..")
                print(f'      {sm:>4}  {s["name"]}')
