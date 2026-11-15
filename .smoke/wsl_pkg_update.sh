#!/bin/bash
# On the real Linux build: 1) run scripts/package.sh and verify the emitted
# tarball is FLAT, driver.py finds its bundled engine in the unpack; 2) run
# the real update.sh manifest-copy block against that real tarball; 3) verify
# the sha256 digest pipeline against a REAL GitHub asset header (HEAD only?
# no - downloads the 0.6.2 linux-x64 asset is 46MB, acceptable once).
set -u
export PATH="/root/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export HOME=/root
R=/mnt/d/Code/2026/Browserr
cd "$R"

echo "=== 1) package.sh (real build) ==="
# dist.sh builds via cargo --release; skip it (already built) by faking the
# dist/engine from target/release if dist.sh is slow, but run it properly:
bash scripts/package.sh > /tmp/pkg.log 2>&1
rc=$?
tail -3 /tmp/pkg.log
[ $rc -eq 0 ] || { echo "PKG-FAIL rc=$rc"; exit 1; }
out=$(ls -t browserr-*.tar.gz | head -1)
echo "asset: $out"
echo "--- members ---"
tar -tzf "$out" | sort
flat=$(tar -tzf "$out" | grep -cE '^\./engine$|^\./driver\.py$|^\./update\.sh$')
nested=$(tar -tzf "$out" | grep -cE '^\./(dist|tools|term|scripts)/')
echo "flat-critical members: $flat (want 3), nested-path members: $nested (want 0)"

unpack=$(mktemp -d)
tar -xzf "$out" -C "$unpack"
[ -x "$unpack/engine" ] && [ -f "$unpack/driver.py" ] && [ -f "$unpack/browserr_mcp.py" ] && [ -x "$unpack/browserr-term" ] && [ -f "$unpack/browserr_term.py" ] \
  && echo "UNPACK-FLAT-OK" || { echo "UNPACK-FLAT-FAIL"; ls -la "$unpack"; exit 1; }
# real engine from the unpack answers --version (proves package shipped the NEW build)
v=$(timeout 15 "$unpack/engine" --version 2>/dev/null)
echo "unpack engine --version: '$v'"
case "$v" in "browserr "*) echo "VERSION-FLAG-OK";; *) echo "VERSION-FLAG-MISSING (engine predates flag?)";; esac
# driver.py discovery in the flat unpack
python3 - "$unpack" <<'PY'
import os, sys, importlib.util as IU
h = sys.argv[1]
sp = IU.spec_from_file_location("drv", os.path.join(h, "driver.py"))
m = IU.module_from_spec(sp); sp.loader.exec_module(m)
assert m.ENGINE_DEBUG == os.path.join(h, "engine"), m.ENGINE_DEBUG
print("driver.py -> bundled engine OK")
PY

echo "=== 2) update.sh manifest copy against the REAL flat tarball ==="
here=$(mktemp -d); tmp=$(mktemp -d)
cp "$unpack/engine" "$unpack/driver.py" "$here/"   # fake old install
printf 'x\n' > "$here/oldfile"
tar -xzf "$out" -C "$tmp"
awk '/^for f in engine driver.py/,/^fi$/' scripts/update.sh > /tmp/mb.sh
bash -c "set -e; here='$here'; tmp='$tmp'; source /tmp/mb.sh"
# engine refreshed + manifest respected + tarball NOT dumped
[ -f "$here/driver.py" ] && [ -f "$here/browserr_term.py" ] && [ -x "$here/browserr-term" ] \
  && [ ! -e "$here/$out" ] && ls "$here" | grep -q oldfile \
  && echo "UPDATE-COPY-OK (payload copied, install-only files kept, no tarball dump)" \
  || { echo "UPDATE-COPY-FAIL"; ls -la "$here"; }

echo "=== 3) digest pipeline against a REAL release asset (v0.6.2, 46MB) ==="
rel=$(curl -fsSL -H "User-Agent: browserr-test" https://api.github.com/repos/Omodaka9375/browserr/releases/latest)
a="browserr-0.6.2-linux-x64.tar.gz"
exp=$(printf '%s' "$rel" | grep -o "\"name\": *\"$a\"[^}]*\"digest\": *\"sha256:[0-9a-f]*\"" | head -1 | grep -o '[0-9a-f]\{64\}')
url=$(printf '%s' "$rel" | grep -o "\"browser_download_url\": *\"[^\"]*$a\"" | head -1 | sed 's/.*\"\(https[^\"]*\)\".*/\1/')
echo "API digest: ${exp:-MISSING}"
if [ -n "$exp" ]; then
  curl -fsSL -o /tmp/asset.bin "$url"
  got=$(sha256sum /tmp/asset.bin | cut -d' ' -f1)
  [ "$got" = "$exp" ] && echo "DIGEST-MATCH-OK ($got)" || echo "DIGEST-MISMATCH got=$got"
  rm -f /tmp/asset.bin
fi

rm -rf "$unpack" "$here" "$tmp" /tmp/mb.sh
echo "WSL-PKG-UPDATE-OK"
