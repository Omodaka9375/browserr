#!/usr/bin/env bash
# Verify the update.sh fail-closed digest gate in isolation (no network):
# extract the exact block from scripts/update.sh and run the three cases.
set -u
cd "$(dirname "$0")/.."

# Pull the digest-gate block: from the mismatch check through the guard
# that follows it. Stop after the SECOND ^fi (the missing-digest one), not
# the first - awk's range would end at the mismatch check's own fi.
awk '/^if \[ -n "\$expected" \] && \[ "\$actual" != "\$expected" \]; then/,0' \
    scripts/update.sh | awk 'BEGIN{n=0} {print} /^fi$/{n++; if(n==2) exit}' \
    > /tmp/digestblock.sh
echo "=== extracted block ==="; cat /tmp/digestblock.sh
echo

run() { # expected actual ALLOW_NO_DIGEST -> prints RESULT:<code>
  ( set -eu
    expected="$1"; actual="$2"
    [ "$3" = unset ] && unset ALLOW_NO_DIGEST || ALLOW_NO_DIGEST="$3"
    source /tmp/digestblock.sh
    echo RESULT:ok-continued
  ) 2>/tmp/digest.err
  echo "RESULT:$? (stderr: $(tr '\n' ' ' </tmp/digest.err | sed 's/  */ /g'))"
}

echo "=== 1) matching digest -> proceed ==="
run "abc123" "abc123" unset
echo "=== 2) mismatched digest -> abort ==="
run "abc123" "deadbeef" unset
echo "=== 3) MISSING digest, no override -> abort (the new fail-closed) ==="
run "" "abc123" unset
echo "=== 4) MISSING digest, ALLOW_NO_DIGEST=1 -> proceed w/ warning ==="
run "" "abc123" 1

rm -f /tmp/digestblock.sh /tmp/digest.err
