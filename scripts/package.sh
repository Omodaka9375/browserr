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
# Flat unpack layout (matches README and the Windows zip): every payload
# file at the archive root, next to the engine. driver.py finds its engine
# next to itself, the TUI launcher looks for the engine beside
# browserr_term.py, and update.sh refuses to run unless the engine is next
# to it - all three need the flat shape. tar cannot rename members
# portably (--transform is GNU-only, macOS ships bsdtar), so stage a flat
# directory and archive its contents.
stage="$(mktemp -d)"
cp "$root/dist/engine" \
   "$root/tools/driver.py" \
   "$root/tools/driver_js.py" \
   "$root/tools/annotate.py" \
   "$root/tools/browserr_mcp.py" \
   "$root/term/browserr_term.py" \
   "$root/term/browserr-term" \
   "$root/scripts/update.sh" \
   "$root/README.md" \
   "$root/LICENSE" \
   "$stage/"
chmod +x "$stage/engine" "$stage/browserr-term" "$stage/update.sh"
tar -czf "$out" -C "$stage" .
rm -rf "$stage"
echo "packaged: $out ($(du -h "$out" | cut -f1))"