"""Release-shape guards: version consistency and updater integrity.

Render-free and offline, so it runs in GPU-less CI too.

Three failure classes this pins down (all found in the wild):

1. VERSION DRIFT. The version lives in five files; a bump that misses one
   ships an engine whose --version disagrees with the wheel, and the
   bootstrap + MCP server then report a permanent "stale engine" warning.
2. UPDATE MANIFEST DRIFT. The updater copies an explicit file list. A file
   added to the release archive but not to that list is silently never
   updated, so an old install keeps an old SDK next to a new engine.
3. SELF-OVERWRITE (found 2026-11, update.sh only). The updater ships inside
   the archive it installs, so it copies a new update.sh over the running
   script. bash re-reads a script by byte offset, so it resumed in the
   middle of the NEW file: every update succeeded and then exited 1 with a
   bogus "asset payload had no engine". The fix is the re-exec guard.
"""
import io
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
failures = []


def read(*parts):
    return io.open(os.path.join(ROOT, *parts), encoding="utf-8",
                   newline="").read()


def check(cond, message):
    if not cond:
        failures.append(message)


# ---- 1. one version, everywhere -----------------------------------------
cargo = re.search(r'(?m)^version\s*=\s*"([^"]+)"', read("engine", "Cargo.toml"))
pyproject = re.search(r'(?m)^version\s*=\s*"([^"]+)"',
                      read("pypi", "pyproject.toml"))
server = json.loads(read("pypi", "server.json"))
mcp = re.search(r'(?m)^MCP_VERSION\s*=\s*"([^"]+)"',
                read("tools", "browserr_mcp.py"))
bootstrap = re.search(r'(?m)^__version__\s*=\s*"([^"]+)"',
                      read("pypi", "src", "browserr_bootstrap", "__init__.py"))

versions = {
    "engine/Cargo.toml": cargo and cargo.group(1),
    "pypi/pyproject.toml": pyproject and pyproject.group(1),
    "pypi/server.json (top)": server.get("version"),
    "pypi/server.json (package)": (server.get("packages") or [{}])[0].get("version"),
    "tools/browserr_mcp.py MCP_VERSION": mcp and mcp.group(1),
    "browserr_bootstrap __version__": bootstrap and bootstrap.group(1),
}
for name, value in versions.items():
    check(value, f"{name}: no version found")
distinct = set(v for v in versions.values() if v)
check(len(distinct) == 1,
      f"version drift across files: {versions}")
check(all(re.fullmatch(r"\d+\.\d+\.\d+", v or "") for v in versions.values()),
      f"not a plain semver version: {versions}")
print("version carriers:", sorted(distinct), "across", len(versions), "spots")


# ---- 2. updater manifest covers the whole archive payload ---------------
def manifest_from_package_sh():
    """Files package.sh copies into the tarball (basenames)."""
    text = read("scripts", "package.sh")
    block = re.search(r'cp "\$root/dist/engine"(.*?)\n\s*"\$stage/"',
                      text, re.S)
    check(block, "package.sh: could not read the payload block")
    names = ["engine"]
    # "$root/<dir>/<file>" tokens; the file part may contain dots.
    for tok in re.findall(r'"\$root/([^"]+)"', block.group(1)):
        names.append(tok.split("/")[-1])
    return names


def manifest_from_package_ps1():
    """Files package.ps1 puts in the zip (basenames)."""
    text = read("scripts", "package.ps1")
    block = re.search(r"\$payload = @\((.*?)\n\)", text, re.S)
    check(block, "package.ps1: could not read the payload block")
    names = []
    for line in block.group(1).splitlines():
        m = re.search(r'Join-Path \$[A-Za-z]+ "([^"]+)"', line)
        if m:
            names.append(m.group(1).replace("\\", "/").split("/")[-1])
    return names


def manifest_from_update_sh():
    text = read("scripts", "update.sh")
    block = re.search(r"for f in (.*?); do", text, re.S)
    check(block, "update.sh: could not read the copy manifest")
    return block.group(1).replace("\\", " ").split()


sh_payload = sorted(set(manifest_from_package_sh()))
ps1_payload = sorted(set(manifest_from_package_ps1()))
upd_payload = sorted(set(manifest_from_update_sh()))

# Each platform archive carries its own launchers: package.ps1 ships the
# Windows ones (mcp_wrap.cmd, browserr-term.cmd, update.cmd), package.sh
# ships the unix ones (browserr-term, update.sh). The updater runs INSIDE an
# unpack, so it only ever sees its own platform's files: compare against the
# unix archive, and require every shared file to be there.
windows_only = {"mcp_wrap.cmd", "browserr-term.cmd", "update.cmd"}
expect_upd = sorted(f for f in sh_payload if f not in windows_only)
for shared in sorted(set(sh_payload) & set(ps1_payload)):
    check(shared in upd_payload or shared in windows_only,
          f"update.sh manifest is missing {shared!r}")
for missing in sorted(set(expect_upd) - set(upd_payload)):
    failures.append(
        f"update.sh does not copy {missing!r}: installs would keep a stale "
        f"copy of it (update.sh manifest: {upd_payload})")
for extra in sorted(set(upd_payload) - set(expect_upd)):
    failures.append(f"update.sh copies {extra!r}, which no packaging "
                    f"script ships")
print("payloads: sh", len(sh_payload), "ps1", len(ps1_payload),
      "updater", len(upd_payload))

# Every file in the archive must also be reachable after an update, so the
# engine plus every python module the SDK imports has to be listed.
for required in ["engine", "driver.py", "driver_js.py", "annotate.py",
                 "browserr_mcp.py"]:
    check(required in upd_payload,
          f"update.sh manifest is missing {required!r}")


# ---- 3. updater self-overwrite guard ------------------------------------
sh = read("scripts", "update.sh")
check("BROWSERR_UPDATE_HOME" in sh and "exec bash" in sh,
      "update.sh lost its re-exec guard: copying the release payload over "
      "the running script makes bash resume at a stale byte offset and "
      "misparse (silent success + exit 1)")
check('cp -- "$0" "$copy"' in sh,
      "update.sh re-exec must copy ITSELF, not the archive copy")
# The guard must come before anything copies files into the install dir.
guard_at = sh.find("BROWSERR_UPDATE_HOME")
first_copy = sh.find('cp -a "$tmp/')
check(guard_at != -1 and (first_copy == -1 or guard_at < first_copy),
      "update.sh must re-exec BEFORE it copies anything into the install")

ps1 = read("scripts", "update.ps1")
# `"a" + "b" -f $x` binds -f to the second string only and prints the
# literal {0}; the fix is to parenthesise the whole concatenation.
for match in re.finditer(r"(?m)^\s*Write-(?:Error|Host) \((.*)\)\s*$", ps1):
    body = match.group(1)
    if "-f " in body and " +" in body and not body.lstrip().startswith("("):
        failures.append(
            "update.ps1: '-f' after a '+' concatenation prints literal "
            f"placeholders: {body.strip()[:70]}")
for literal in re.findall(r'\(\(("[^"]*"\s*\+\s*"[^"]*"\)\s*-f )', ps1):
    check(True, literal)
guarded = re.search(r"Write-Error \(\(\"engine is running \(pid \{0\}\)", ps1)
check(guarded, "update.ps1: the running-engine message lost its -f guard "
               "(it printed a literal '{0}' instead of the pid)")

if failures:
    print("\nFAILED:")
    for f in failures:
        print(" -", f)
    raise SystemExit(1)
print("ALL OK")
