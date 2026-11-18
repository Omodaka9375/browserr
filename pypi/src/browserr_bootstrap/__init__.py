"""Browserr MCP bootstrap: one-command install of the agent browser.

The MCP server itself is stdlib-only Python shipped in the Browserr
release archives; this package exists so an MCP client can be configured
with a single command (`browserr-mcp` / `uvx browserr-mcp`) and the
platform engine binary is fetched and verified automatically on first
run.

How it works (all stdlib):

1. Resolve the engine dir:
   - $BROWSERR_HOME if set (explicit override),
   - a pinned $BROWSERR_VERSION,
   - else the cached/latest release under ~/.browserr/versions/<tag>
     (per-version dirs, atomic updates).
2. If the engine binary is missing there, download the platform asset
   from the official GitHub release, verify its sha256 against the
   digest GitHub reports for the asset, and unpack it in place.
3. Exec browserr_mcp.py from the unpack in-process (runpy): it speaks
   newline JSON-RPC on stdio and needs no third-party packages.

Offline rules (an installed engine never depends on the GitHub API):
- $BROWSERR_HOME or a pinned $BROWSERR_VERSION that is already on disk
  starts with no network call at all.
- Otherwise the resolved release tag is cached in
  ~/.browserr/latest.json for CACHE_TTL seconds; a fresh cache starts
  with no network call, and a stale cache triggers one best-effort
  refresh per start that falls back to the newest installed engine
  when the API is unreachable (rate limit, offline, bad response)
  instead of dying. $BROWSERR_UPDATE=never disables the refresh.

Fail-closed rules (the whole product is fail-closed; so is this):
- No digest published for the asset -> refuse to install (the digest is
  the corruption/asset-swap guard; TLS only covers the transport).
- Non-GitHub download URL -> refuse (a broken API response cannot send
  the download off-site).
- Unreasonably large asset -> refuse.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import runpy
import shutil
import stat
import sys
import tempfile
import time
import urllib.request
import zipfile

__version__ = "0.9.1"

REPO = "Omodaka9375/browserr"
API = "https://api.github.com"
MAX_ASSET_BYTES = 500 * 1024 * 1024  # sanity ceiling, matches update.ps1/sh
ALLOWED_HOSTS = {"github.com", "objects.githubusercontent.com"}
CACHE_TTL = 24 * 3600


def _platform_asset(version: str) -> str:
    machine = platform.machine().lower()
    system = platform.system().lower()
    if system == "windows":
        arch = "x64" if machine in ("amd64", "x86_64") else None
        ext = "zip"
    elif system == "linux":
        arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}.get(machine)
        ext = "tar.gz"
        # Termux (Android): Python runs under Linux/aarch64 but the engine
        # asset is the android build. $PREFIX ends in /com.termux/files/usr.
        if os.path.isdir("/data/data/com.termux/files/usr"):
            system = "android"
    elif system == "darwin":
        # Release assets are labeled macos-* (not darwin-*); platform.system()
        # says "darwin" on a Mac, so map it or the asset name never matches.
        system = "macos"
        arch = "arm64" if machine == "arm64" else "x64"
        ext = "tar.gz"
    else:
        arch = None
    if not arch:
        raise SystemExit(
            f"browserr-mcp: no release asset for {system}/{machine}")
    return f"browserr-{version}-{system}-{arch}.{ext}"


def _get_json(url: str):
    req = urllib.request.Request(url, headers={
        "User-Agent": "browserr-mcp-bootstrap",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _resolve_release() -> tuple[str, dict]:
    pin = os.environ.get("BROWSERR_VERSION", "").strip().lstrip("v")
    if pin:
        rel = _get_json(f"{API}/repos/{REPO}/releases/tags/v{pin}")
    else:
        rel = _get_json(f"{API}/repos/{REPO}/releases/latest")
    tag = rel.get("tag_name", "").lstrip("v")
    if not tag:
        raise SystemExit("browserr-mcp: release has no tag_name")
    return tag, rel


def _versions_base() -> str:
    return os.path.join(os.path.expanduser("~"), ".browserr", "versions")


def _install_dir(version: str) -> str:
    home = os.environ.get("BROWSERR_HOME")
    if home:
        return os.path.abspath(home)
    return os.path.join(_versions_base(), version)


def _cache_path() -> str:
    return os.path.join(os.path.dirname(_versions_base()), "latest.json")


def _read_cache() -> dict | None:
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            c = json.load(f)
        tag, checked = c.get("tag"), c.get("checked")
        if isinstance(tag, str) and tag and isinstance(checked, (int, float)):
            return {"tag": tag.lstrip("v"), "checked": checked}
    except (OSError, ValueError):
        pass
    return None


def _write_cache(tag: str) -> None:
    path = _cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"tag": tag, "checked": time.time()}, f)
    os.replace(tmp, path)


def _engine_present(d: str) -> bool:
    exe = "engine.exe" if os.name == "nt" else "engine"
    return os.path.isfile(os.path.join(d, exe))


def _newest_installed() -> str | None:
    """Newest version dir under ~/.browserr/versions that holds an engine."""
    def key(name: str):
        parts = name.lstrip("v").split(".")
        nums = []
        for p in parts:
            try:
                nums.append(int(p))
            except ValueError:
                nums.append(0)
        while len(nums) < 3:
            nums.append(0)
        return nums
    best = None
    try:
        names = os.listdir(_versions_base())
    except OSError:
        return None
    for name in names:
        d = os.path.join(_versions_base(), name)
        if _engine_present(d) and (best is None or key(name) > key(best)):
            best = name
    return best


def _unpack(path: str, dest: str) -> None:
    if path.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            z.extractall(dest)
            for info in z.infolist():
                if info.external_attr >> 28 == 0o3:
                    target = os.path.join(dest, info.filename)
                    if os.path.exists(target):
                        os.chmod(target, os.stat(target).st_mode | stat.S_IXUSR)
    else:
        import tarfile
        with tarfile.open(path) as t:
            t.extractall(dest)
            for m in t.getmembers():
                if m.mode & 0o111 and m.isfile():
                    target = os.path.join(dest, m.name)
                    if os.path.exists(target):
                        os.chmod(target, os.stat(target).st_mode | stat.S_IXUSR)


def _download(tag: str, release: dict, dest: str) -> str:
    """Download+verify the platform asset of `tag` into `dest`; return dest."""
    asset_name = _platform_asset(tag)
    asset = next((a for a in release.get("assets", [])
                  if a.get("name") == asset_name), None)
    if not asset:
        raise SystemExit(f"browserr-mcp: release v{tag} has no asset {asset_name}")

    url = asset.get("browser_download_url", "")
    host = urllib.request.urlparse(url).hostname or ""
    if urllib.request.urlparse(url).scheme != "https" or host not in ALLOWED_HOSTS:
        raise SystemExit(f"browserr-mcp: refusing non-GitHub download URL: {url}")
    if asset.get("size", 0) > MAX_ASSET_BYTES:
        raise SystemExit("browserr-mcp: refusing implausibly large asset")

    expected = (asset.get("digest") or "").removeprefix("sha256:").lower()
    if not expected:
        raise SystemExit(
            "browserr-mcp: release asset carries no sha256 digest; refusing "
            "to install unverified (the digest is the corruption guard; "
            "TLS only covers the transport)")

    os.makedirs(dest, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="browserr-bootstrap-")
    try:
        archive = os.path.join(tmp, asset_name)
        print(f"browserr-mcp: downloading {asset_name} "
              f"({asset.get('size', 0) // (1024*1024)} MB)...", file=sys.stderr)
        req = urllib.request.Request(url, headers={"User-Agent": "browserr-mcp-bootstrap"})
        with urllib.request.urlopen(req, timeout=600) as r, open(archive, "wb") as f:
            shutil.copyfileobj(r, f)

        h = hashlib.sha256()
        with open(archive, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        actual = h.hexdigest()
        if actual != expected:
            raise SystemExit(f"browserr-mcp: sha256 mismatch: expected {expected}, got {actual}")

        # unpack to a staging dir, then move into place (all-or-nothing)
        stage = os.path.join(tmp, "unpacked")
        os.makedirs(stage)
        _unpack(archive, stage)
        if not _engine_present(stage):
            raise SystemExit("browserr-mcp: archive has no engine binary; not a browserr release?")
        for entry in os.listdir(stage):
            shutil.move(os.path.join(stage, entry), os.path.join(dest, entry))
        return dest
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def ensure_engine() -> str:
    """Return the unpack dir; download+verify the engine if missing.

    Disk first: an installed engine starts without touching the
    network. $BROWSERR_HOME and a pinned $BROWSERR_VERSION are full
    offline skips. The default path caches the resolved release tag for
    CACHE_TTL seconds; a stale cache triggers one best-effort refresh
    that falls back to the newest installed engine when the API is
    unreachable (rate limit, offline) instead of dying.
    """
    home = os.environ.get("BROWSERR_HOME", "").strip()
    if home:
        dest = os.path.abspath(home)
        if _engine_present(dest):
            return dest
        tag, release = _resolve_release()
        return _download(tag, release, dest)

    pin = os.environ.get("BROWSERR_VERSION", "").strip().lstrip("v")
    if pin:
        dest = os.path.join(_versions_base(), pin)
        if _engine_present(dest):
            return dest
        tag, release = _resolve_release()
        dest = os.path.join(_versions_base(), tag)
        return dest if _engine_present(dest) else _download(tag, release, dest)

    never = os.environ.get("BROWSERR_UPDATE", "").strip().lower() == "never"

    cache = _read_cache()
    cached_dir = None
    if cache:
        d = os.path.join(_versions_base(), cache["tag"])
        cached_dir = d if _engine_present(d) else None

    if cached_dir and (never or time.time() - cache["checked"] < CACHE_TTL):
        return cached_dir

    installed = _newest_installed()
    if never and installed:
        return os.path.join(_versions_base(), installed)

    try:
        tag, release = _resolve_release()
    except Exception as e:
        installed = installed if installed is not None else _newest_installed()
        if installed:
            print(f"browserr-mcp: release check failed ({e}); "
                  "starting the installed engine", file=sys.stderr)
            return os.path.join(_versions_base(), installed)
        raise SystemExit(
            "browserr-mcp: cannot resolve a release and no engine is "
            f"installed ({e})")
    _write_cache(tag)

    dest = os.path.join(_versions_base(), tag)
    return dest if _engine_present(dest) else _download(tag, release, dest)


def main() -> None:
    """MCP server entry point: ensure the engine, then run browserr_mcp."""
    dest = ensure_engine()
    mcp = os.path.join(dest, "browserr_mcp.py")
    if not os.path.isfile(mcp):
        raise SystemExit(f"browserr-mcp: browserr_mcp.py missing in {dest}")
    os.environ["BROWSERR_ENGINE"] = os.path.join(
        dest, "engine.exe" if os.name == "nt" else "engine")
    sys.argv = [mcp] + sys.argv[1:]
    sys.path.insert(0, dest)
    runpy.run_path(mcp, run_name="__main__")


def install_main() -> None:
    """Standalone installer entry point: download + verify, then exit."""
    tag, release = _resolve_release()
    dest = _install_dir(tag)
    if _engine_present(dest):
        print(f"browserr-install: v{tag} already present at {dest}")
    else:
        _download(tag, release, dest)
        print(f"browserr-install: v{tag} installed at {dest}")
    if not os.environ.get("BROWSERR_HOME"):
        _write_cache(tag)
    print("MCP config: uvx browserr-mcp (or pip install browserr-mcp "
          "then command browserr-mcp)")