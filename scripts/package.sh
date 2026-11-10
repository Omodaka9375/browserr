#!/usr/bin/env bash
# Package a release tarball (Linux/macOS): builds the engine (release),
# then archives the engine binary, the Python SDK, the MCP server, README
# and LICENSE into browserr-<version>-<platform>.tar.gz.
# Version comes from RELEASE_VERSION (CI passes the git tag), else Cargo.toml.
set -eu
cd "$(dirname "$0")/.."
root="$PWD"

bash scripts/dist.sh

if [ -n "${RELEASE_VERSION:-}" ]; then
    version="${RELEASE_VERSION#v}"
else
    version=$(grep -m1 '^version' engine/Cargo.toml | sed 's/.*"\(.*\)".*/\1/')
fi

platform="$(uname -s | tr 'A-Z' 'a-z')-$(uname -m)"
out="browserr-$version-$platform.tar.gz"
rm -f "$out"
tar -czf "$out" \
    -C "$root" \
    dist/engine \
    tools/driver.py \
    tools/browserr_mcp.py \
    README.md \
    LICENSE
echo "packaged: $out ($(du -h "$out" | cut -f1))"