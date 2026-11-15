#!/usr/bin/env bash
# Update a Browserr unpack in place to the latest GitHub release.
#
# Runs from inside the unpack (the engine binary next to this script).
# Downloads the platform asset, verifies it against the sha256 digest
# GitHub reports for the asset, then extracts the payload over this
# directory. Needs: curl (or wget), unzip or tar, sha256sum or shasum.
#
# Usage: ./update.sh [tag]     (default: latest release)
set -eu

here="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repo="Omodaka9375/browserr"
exe="$here/engine"
[ -f "$exe" ] || { echo "no engine next to this script; run update from inside the unpack" >&2; exit 1; }

if command -v curl >/dev/null 2>&1; then fetch() { curl -fsSL -H "$1" "$2"; }
elif command -v wget >/dev/null 2>&1; then fetch() { wget -qO- --header="$1" "$2"; }
else echo "need curl or wget" >&2; exit 1; fi

if command -v sha256sum >/dev/null 2>&1; then hash() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then hash() { shasum -a 256 "$1" | cut -d' ' -f1; }
else echo "need sha256sum or shasum" >&2; exit 1; fi

if [ -f "$here/unzip" ] || command -v unzip >/dev/null 2>&1; then UNZIP=1; fi

version="$("$exe" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || true)"
version="${version:-unknown}"  # pre-0.6 archives have no --version

api="https://api.github.com/repos/$repo/releases/latest"
[ -n "${1:-}" ] && api="https://api.github.com/repos/$repo/releases/tags/$1"

release=$(fetch "User-Agent: browserr-update" "$api")
tag=$(printf '%s' "$release" | grep -o '"tag_name": *"[^"]*"' | head -1 | sed 's/.*"\(v\{0,1\}\)"; */\1/; s/.*"\(v[0-9.]*\|[0-9.]*\)".*/\1/' | sed 's/^v//')
[ -n "$tag" ] || { echo "could not read latest release tag" >&2; exit 1; }

if [ "$tag" = "$version" ]; then
    echo "already up to date ($version)"
    exit 0
fi

# Platform label matches the release asset names.
os=$(uname -s); arch=$(uname -m)
case "$os" in Darwin) os=macos ;; Linux) os=linux ;; esac
case "$arch" in x86_64|amd64) arch=x64 ;; aarch64|arm64) arch=arm64 ;; esac
asset="browserr-$tag-$os-$arch.tar.gz"
url=$(printf '%s' "$release" | grep -o "\"browser_download_url\": *\"[^\"]*$asset\"" | head -1 | sed 's/.*"\(https[^"]*\)".*/\1/')
[ -n "$url" ] || { echo "release $tag has no asset $asset" >&2; exit 1; }

echo "updating $version -> $tag"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fsSL -H "User-Agent: browserr-update" -o "$tmp/$asset" "$url" 2>/dev/null \
    || wget -qO "$tmp/$asset" --header="User-Agent: browserr-update" "$url"

expected=$(printf '%s' "$release" | grep -o "\"name\": *\"$asset\"[^}]*\"digest\": *\"sha256:[0-9a-f]*\"" | head -1 | grep -o '[0-9a-f]\{64\}' || true)
actual=$(hash "$tmp/$asset")
if [ -n "$expected" ] && [ "$actual" != "$expected" ]; then
    echo "sha256 mismatch: expected $expected got $actual" >&2
    exit 1
fi
[ -n "$expected" ] || echo "warning: release carries no digest; installed without verification" >&2

backup="$here/backup-$version.tmp"
rm -rf "$backup"
mkdir -p "$backup"
cp -a "$here/." "$backup/"

tar -xzf "$tmp/$asset" -C "$tmp"
cp -a "$tmp/dist/engine" "$here/" 2>/dev/null || cp -a "$tmp/." "$here/"

newversion="$("$exe" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || true)"
case "$newversion" in
    "$tag") rm -rf "$backup"; echo "updated to $tag (backup removed)" ;;
    *) echo "updated binary reports '${newversion:-none}', expected $tag; backup kept at $backup" >&2; exit 1 ;;
esac
