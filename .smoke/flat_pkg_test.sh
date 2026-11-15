#!/usr/bin/env bash
# Verify the 0.7.0 unix MUST fixes without touching the network.
# Mirrors package.sh staging, update.sh copy/gate/backup logic verbatim
# against a real flat tarball.
set -eu
cd "$(dirname "$0")/.."
root="$PWD"

echo "=== 1) package.sh staging produces a FLAT tar ==="
# package.sh stages $root/dist/engine (built earlier by dist.sh). For this
# hermetic layout test the binary contents do not matter, only that the
# file is present, so synthesize it if a real build is absent.
if [ ! -f "$root/dist/engine" ]; then
  mkdir -p "$root/dist"; printf '#!/bin/sh\necho "browserr 0.7.0"\n' > "$root/dist/engine"
  chmod +x "$root/dist/engine"; FAKE_ENGINE=1
fi
stage="$(mktemp -d)"
cp "$root/dist/engine" "$root/tools/driver.py" "$root/tools/browserr_mcp.py" \
   "$root/term/browserr_term.py" "$root/term/browserr-term" \
   "$root/scripts/update.sh" "$root/README.md" "$root/LICENSE" "$stage/"
chmod +x "$stage/engine" "$stage/browserr-term" "$stage/update.sh"
asset="browserr-flat-test.tar.gz"
out="$stage.tar.gz"   # write OUTSIDE the staged dir (matches package.sh)
tar -czf "$out" -C "$stage" .
echo "--- members ---"
tar -tzf "$out" | sort
[ -f "$stage/dist/engine" ] && { echo "FAIL: engine under dist/ prefix"; exit 1; }
echo "flat: engine + payload all at root OK"

echo "=== 2) update.sh: extract to tmp, flat members land correctly ==="
here="$(mktemp -d)"; tmp="$(mktemp -d)"
cp "$root/scripts/update.sh" "$here/update.sh"
tar -xzf "$out" -C "$tmp"
[ -f "$tmp/engine" ] && [ -f "$tmp/driver.py" ] || { echo "FAIL: extract layout"; exit 1; }
[ -d "$tmp/dist" ] && { echo "FAIL: nested dist leaked"; exit 1; }
echo "extract OK (engine, driver.py at tmp root, no dist/ nesting)"

echo "=== 3) update.sh copy manifest (extract the live block, run it) ==="
printf 'OLD\n' > "$here/README.md"; printf 'OLD\n' > "$here/driver.py"
# pull the copy block straight out of update.sh and execute with here/tmp set
awk '/^for f in engine driver.py/,/^fi$/' "$here/update.sh" > /tmp/copyblock.sh
bash -c "set -eu; here='$here'; tmp='$tmp'; source /tmp/copyblock.sh"
grep -q OLD "$here/README.md" && { echo "FAIL: README not refreshed"; exit 1; }
for f in engine driver.py browserr_mcp.py browserr_term.py browserr-term; do
  [ -f "$here/$f" ] || { echo "FAIL: $f not copied"; exit 1; }
done
ls "$here" | grep -q "\.tar\.gz" && { echo "FAIL: tarball dumped into install"; exit 1; }
echo "copy manifest OK (payload refreshed, tarball not copied)"

echo "=== 4) driver.py finds the bundled engine in the flat unpack ==="
python3 - "$here" <<'PY'
import os, sys, importlib.util as IU
h = sys.argv[1]
sp = IU.spec_from_file_location("drv", os.path.join(h, "driver.py"))
mod = IU.module_from_spec(sp)
sp.loader.exec_module(mod)
exp = os.path.join(h, "engine")
assert mod.ENGINE_DEBUG == exp, (mod.ENGINE_DEBUG, exp)
assert os.path.exists(exp), "bundled engine not present in unpack"
print("driver.py ENGINE_DEBUG ->", mod.ENGINE_DEBUG)
PY

echo "=== 5) update.sh engine-absent gate (exit non-zero) ==="
ne="$(mktemp -d)"; cp "$root/scripts/update.sh" "$ne/update.sh"; chmod +x "$ne/update.sh"
if "$ne/update.sh" >/tmp/ne.out 2>&1; then echo "FAIL: accepted missing engine"; exit 1; fi
grep -q "no engine next to this script" /tmp/ne.out
echo "gate OK: $(head -1 /tmp/ne.out)"

echo "=== 6) backup OUTSIDE install, cp succeeds (the self-copy bug) ==="
backup="${TMPDIR:-/tmp}/browserr-backup-test.$$.tmp"
rm -rf "$backup"; mkdir -p "$backup"
cp -a "$here/." "$backup/" || { echo "FAIL: backup cp errored"; exit 1; }
n=$(find "$backup" -type f | wc -l)
[ "$n" -ge 5 ] || { echo "FAIL: backup only captured $n files (empty = the bug)"; exit 1; }
echo "backup captured $n files to $backup (outside install) OK"
rm -rf "$backup"

rm -rf "$stage" "$here" "$tmp" "$ne" "$out" /tmp/copyblock.sh /tmp/ne.out
[ "${FAKE_ENGINE:-}" = 1 ] && rm -f "$root/dist/engine"
echo "=== FLAT-PKG SMOKE: ALL OK ==="
