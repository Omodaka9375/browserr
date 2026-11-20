"""Bootstrap platform-asset mapping vs the published release assets.

The 0.9.1 macOS breakage: platform.system() says "darwin" while release
assets are labeled macos-*, so the download never matched. This test pins
every platform mapping to the exact asset-name contract (the globs in
.github/workflows/release.yml). Render-free: no engine, no network.
"""
import os
import sys
from unittest import mock

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(TOOLS), "pypi", "src"))
import browserr_bootstrap as bs

V = "0.9.2"

# (system, machine, expected asset, termux-dir-present)
CASES = [
    ("Windows", "AMD64", f"browserr-{V}-windows-x64.zip", False),
    ("Linux", "x86_64", f"browserr-{V}-linux-x64.tar.gz", False),
    ("Linux", "aarch64", f"browserr-{V}-linux-arm64.tar.gz", False),
    ("Darwin", "arm64", f"browserr-{V}-macos-arm64.tar.gz", False),
    # darwin x64 is not built today but the name must still follow the
    # contract (release.yml globs browserr-*-macos-*.tar.gz).
    ("Darwin", "x86_64", f"browserr-{V}-macos-x64.tar.gz", False),
    # Termux: Python reports Linux/aarch64 but the asset is android-arm64.
    ("Linux", "aarch64", f"browserr-{V}-android-arm64.tar.gz", True),
]


def fake_paths(termux):
    def isdir(p):
        return bool(termux) and str(p).startswith("/data/data/com.termux")
    return isdir


for system, machine, expected, termux in CASES:
    with mock.patch.object(bs.platform, "system", return_value=system), \
         mock.patch.object(bs.platform, "machine", return_value=machine), \
         mock.patch.object(bs.os.path, "isdir", side_effect=fake_paths(termux)):
        got = bs._platform_asset(V)
    assert got == expected, f"{system}/{machine} termux={termux}: {got} != {expected}"
    print("asset mapping OK:", got)

# Unsupported platforms fail loudly, never with a guessed name.
for system, machine in (("FreeBSD", "amd64"), ("Windows", "arm64")):
    with mock.patch.object(bs.platform, "system", return_value=system), \
         mock.patch.object(bs.platform, "machine", return_value=machine):
        try:
            bs._platform_asset(V)
        except SystemExit:
            print("unsupported platform rejected OK:", system, machine)
        else:
            raise AssertionError(f"{system}/{machine} should have failed")

# The names must match the release workflow's publish globs.
GLOBS = [
    ("browserr-*-windows-x64.zip", True),
    ("browserr-*-linux-x64.tar.gz", True),
    ("browserr-*-linux-arm64.tar.gz", True),
    ("browserr-*-macos-*.tar.gz", True),
    ("browserr-*-android-arm64.tar.gz", True),
]


def glob_match(pattern, name):
    import re
    rx = re.escape(pattern).replace(r"\*", ".*")
    return re.fullmatch(rx, name) is not None


for _, expected, termux in [(c[0], c[2], c[3]) for c in CASES]:
    assert any(glob_match(g, expected) for g, _ in GLOBS), \
        f"{expected} matches no release glob"
print("release-glob contract OK")
print("ALL OK")
