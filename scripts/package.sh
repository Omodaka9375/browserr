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

# Arch labels follow the release workflow's asset names (x64, not x86_64).
arch="$(uname -m)"
case "$arch" in
    x86_64|amd64) arch="x64" ;;
    aarch64|arm64) arch="arm64" ;;
esac
os="$(uname -s)"
case "$os" in
    Darwin) os="macos" ;;
    Linux)  os="linux" ;;
esac
platform="$os-$arch"
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