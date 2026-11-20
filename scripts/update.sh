#!/usr/bin/env bash
# Update a Browserr unpack in place to the latest GitHub release.
#
# Runs from inside the unpack (the engine binary next to this script).
# Downloads the platform asset, verifies it against the sha256 digest
# GitHub reports for the asset, then extracts the payload over this
# directory. Needs: curl (or wget), tar, sha256sum or shasum.
#
# Usage: ./update.sh [tag]     (default: latest release)
set -eu

# bash reads a script incrementally BY BYTE OFFSET, so a script that copies
# the release payload over itself (update.sh IS in the payload) resumes
# reading the middle of the NEW file after that copy and misparses: a fully
# successful update then exits 1 with a bogus "asset payload had no engine".
# Re-exec from a private copy so the installed update.sh is free to be
# replaced. (update.ps1 is immune: PowerShell parses the whole script first.)
if [ -z "${BROWSERR_UPDATE_HOME:-}" ]; then
    BROWSERR_UPDATE_HOME="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
    export BROWSERR_UPDATE_HOME
    copy="$(mktemp "${TMPDIR:-/tmp}/browserr-update-XXXXXX")"
    cp -- "$0" "$copy"
    BROWSERR_UPDATE_TMP_SCRIPT="$copy"
    export BROWSERR_UPDATE_TMP_SCRIPT
    exec bash "$copy" "$@"
fi

tmp=""
cleanup() {
    [ -n "$tmp" ] && rm -rf "$tmp"
    [ -n "${BROWSERR_UPDATE_TMP_SCRIPT:-}" ] && rm -f -- "$BROWSERR_UPDATE_TMP_SCRIPT"
}
trap cleanup EXIT

here="$BROWSERR_UPDATE_HOME"
repo="Omodaka9375/browserr"
exe="$here/engine"
[ -f "$exe" ] || { echo "no engine next to this script; run update from inside the unpack" >&2; exit 1; }

if command -v curl >/dev/null 2>&1; then fetch() { curl -fsSL -H "$1" "$2"; }
elif command -v wget >/dev/null 2>&1; then fetch() { wget -qO- --header="$1" "$2"; }
else echo "need curl or wget" >&2; exit 1; fi

if command -v sha256sum >/dev/null 2>&1; then hash() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then hash() { shasum -a 256 "$1" | cut -d' ' -f1; }
else echo "need sha256sum or shasum" >&2; exit 1; fi

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
# The URL comes from the same API response as the digest, so this is not a
# MITM defence (TLS is); it just pins the host so a broken/odd response
# cannot send the download off-site. (The sed above already forces https.)
case "$url" in
    https://github.com/*|https://objects.githubusercontent.com/*) ;;
    *) echo "refusing non-GitHub download URL: $url" >&2; exit 1 ;;
esac

echo "updating $version -> $tag"

tmp=$(mktemp -d)
curl -fsSL -H "User-Agent: browserr-update" -o "$tmp/$asset" "$url" 2>/dev/null \
    || wget -qO "$tmp/$asset" --header="User-Agent: browserr-update" "$url"

# Pull this asset's digest out of the release JSON with grep (no jq
# dependency). Anchor on digest -> browser_download_url, NOT name ->
# digest: the asset's nested "uploader":{...} object closes with a '}'
# between the name and digest fields, so a "[^}]*" span starting at name
# never reaches digest and always returns empty. Between digest and
# browser_download_url there are only scalar fields, so [^}]* stays inside
# one asset object and cannot leak into the next.
expected=$(printf '%s' "$release" | grep -o "\"digest\": *\"sha256:[0-9a-f]*\"[^}]*\"browser_download_url\": *\"[^\"]*$asset\"" | head -1 | grep -o '[0-9a-f]\{64\}' || true)
actual=$(hash "$tmp/$asset")
if [ -n "$expected" ] && [ "$actual" != "$expected" ]; then
    echo "sha256 mismatch: expected $expected got $actual" >&2
    exit 1
fi
# A missing digest aborts too: the digest is the only corruption/asset-swap
# guard (TLS covers the transport, not what GitHub stored). Override
# explicitly with ALLOW_NO_DIGEST=1 if you must install an unverified asset.
if [ -z "$expected" ]; then
    if [ -z "${ALLOW_NO_DIGEST:-}" ]; then
        echo "release carries no sha256 digest; refusing to install " \
             "unverified (set ALLOW_NO_DIGEST=1 to override)" >&2
        exit 1
    fi
    echo "warning: no digest published; installing unverified" >&2
fi

# The backup must live OUTSIDE the install: cp -a "$here/." "$here/..."
# copies a directory into its own child, which GNU cp refuses (and, worse,
# returns exit 0 while doing so, so set -e never fires and the "backup"
# silently stays empty). Match update.ps1, which uses %TEMP%. PID-suffixed
# so concurrent runs cannot share one backup.
backup="${TMPDIR:-/tmp}/browserr-backup-$version.$$.tmp"
rm -rf "$backup"
mkdir -p "$backup"
cp -a "$here/." "$backup/" || { echo "backup to $backup failed" >&2; exit 1; }

tar -xzf "$tmp/$asset" -C "$tmp"
# Flat archive layout (see package.sh): every payload file sits at the
# archive root. Copy the known manifest only - never the whole extraction
# tree, which also holds the downloaded asset itself.
for f in engine driver.py driver_js.py annotate.py browserr_mcp.py \
         browserr_term.py \
         browserr-term update.sh README.md LICENSE; do
    if [ -f "$tmp/$f" ]; then cp -a "$tmp/$f" "$here/"; fi
done
if [ ! -f "$here/engine" ]; then
    echo "asset payload had no engine; backup kept at $backup" >&2
    exit 1
fi

newversion="$("$exe" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || true)"
case "$newversion" in
    "$tag") rm -rf "$backup"; echo "updated to $tag (backup removed)" ;;
    *) echo "updated binary reports '${newversion:-none}', expected $tag; backup kept at $backup" >&2; exit 1 ;;
esac
