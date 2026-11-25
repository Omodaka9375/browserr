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

# _unpack must preserve nested directory entries (zip and tar) and reject
# path traversal / symlinks.
import io
import shutil
import tarfile
import tempfile
import zipfile

tmp = tempfile.mkdtemp(prefix="browserr-test-unpack-")
try:
    # 1) Nested zip with explicit directory entry
    zp = os.path.join(tmp, "ok.zip")
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("sub/", "")
        z.writestr("sub/nested/hello.txt", "hi")
    out_z = os.path.join(tmp, "out-z")
    bs._unpack(zp, out_z)
    with open(os.path.join(out_z, "sub", "nested", "hello.txt")) as f:
        assert f.read() == "hi"
    print("zip nested-dir unpack OK")

    # 2) Nested tar.gz with explicit directory entry
    tp = os.path.join(tmp, "ok.tar.gz")
    with tarfile.open(tp, "w:gz") as t:
        d = tarfile.TarInfo("sub/")
        d.type = tarfile.DIRTYPE
        t.addfile(d)
        f = tarfile.TarInfo("sub/hello.txt")
        data = b"hi"
        f.size = len(data)
        t.addfile(f, io.BytesIO(data))
    out_t = os.path.join(tmp, "out-t")
    bs._unpack(tp, out_t)
    with open(os.path.join(out_t, "sub", "hello.txt"), "rb") as f:
        assert f.read() == b"hi"
    print("tar nested-dir unpack OK")

    # 3) Path traversal rejected in both zip and tar
    for bad in ("../escape.txt", "/abs.txt", "sub/../../escape.txt"):
        try:
            bs._safe_member_name(bad)
        except SystemExit:
            pass
        else:
            raise AssertionError(f"should have rejected {bad!r}")

    # 4) Symlink in tar rejected
    tp_sym = os.path.join(tmp, "sym.tar.gz")
    with tarfile.open(tp_sym, "w:gz") as t:
        s = tarfile.TarInfo("link")
        s.type = tarfile.SYMTYPE
        s.linkname = "/etc/passwd"
        t.addfile(s)
    try:
        bs._unpack(tp_sym, os.path.join(tmp, "out-sym"))
    except SystemExit:
        print("tar symlink rejected OK")
    else:
        raise AssertionError("symlink in tar should have been rejected")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("ALL OK")
