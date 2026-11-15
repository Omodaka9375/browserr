#!/bin/bash
# Drift-proof: pull the EXACT `expected=` line out of production update.sh
# and eval it verbatim against the live GitHub API, for every real asset.
export PATH="/usr/bin:/bin"
R=/mnt/d/Code/2026/Browserr
line=$(grep -F 'expected=$(printf' "$R/scripts/update.sh")
[ -n "$line" ] || { echo "could not find expected= line in update.sh"; exit 1; }
echo "=== production line under test ==="
echo "$line"
echo

release=$(curl -fsSL -H "User-Agent: browserr-test" https://api.github.com/repos/Omodaka9375/browserr/releases/latest)

# ground truth via python (json-aware)
printf '%s' "$release" > /tmp/rel.json
declare -A truth
while IFS='|' read -r n d; do truth["$n"]="$d"; done < <(python3 - <<'PY'
import json
r=json.load(open('/tmp/rel.json'))
for a in r['assets']:
    print(f"{a['name']}|{a.get('digest','')}")
PY
)

pass=0; fail=0
for a in "${!truth[@]}"; do
  asset="$a"
  eval "$line"   # sets $expected from the production pipeline
  want="${truth[$a]#sha256:}"
  if [ "$expected" = "$want" ]; then
    printf 'MATCH   %-45s %s\n' "$a" "$expected"; pass=$((pass+1))
  else
    printf 'MISMATCH %-44s got=%s want=%s\n' "$a" "${expected:-EMPTY}" "$want"; fail=$((fail+1))
  fi
done
echo "regex match: $pass pass, $fail fail"

echo "=== end-to-end: real download, production hash+digest compare (linux-x64) ==="
asset="browserr-0.6.2-linux-x64.tar.gz"
eval "$line"
url=$(printf '%s' "$release" | grep -o "\"browser_download_url\": *\"[^\"]*$asset\"" | head -1 | sed 's/.*"\(https[^"]*\)".*/\1/')
curl -fsSL -o /tmp/asset.bin "$url"
# use the same hash helper logic as update.sh
if command -v sha256sum >/dev/null; then actual=$(sha256sum /tmp/asset.bin | cut -d' ' -f1); else actual=$(shasum -a 256 /tmp/asset.bin | cut -d' ' -f1); fi
echo "expected(API digest): $expected"
echo "actual(downloaded)   : $actual"
[ "$expected" = "$actual" ] && echo "E2E-DIGEST-OK (download verifies)" || echo "E2E-DIGEST-FAIL"
rm -f /tmp/rel.json /tmp/asset.bin
[ "$fail" -eq 0 ] && echo "PROD-DIGEST-VERIFY-OK" || echo "PROD-DIGEST-VERIFY-FAILED"
