#!/usr/bin/env bash
# Verify the update.sh download-URL host guard (NIT) by extracting the real
# case block from scripts/update.sh and running it against sample URLs.
set -u
cd "$(dirname "$0")/.."
awk '/^case "\$url" in/,/^esac$/' scripts/update.sh > /tmp/urlblock.sh
echo "=== extracted guard ==="; cat /tmp/urlblock.sh; echo

verdict() { out=$( ( url="$1"; source /tmp/urlblock.sh; echo ALLOW ) 2>&1 ); \
            case "$out" in *ALLOW*) echo "ALLOW";; *) echo "DENY";; esac; }

for u in \
  "https://github.com/O/R/releases/download/v0.7.0/a.tar.gz" \
  "https://objects.githubusercontent.com/asset/xyz" \
  "https://evil.com/payload.tar.gz" \
  "https://github.com.evil.com/x" ; do
  printf '%-70s -> %s\n' "$u" "$(verdict "$u")"
done
rm -f /tmp/urlblock.sh
