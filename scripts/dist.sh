#!/usr/bin/env bash
# Build the engine (release) into dist/ (Linux/macOS).
set -eu
cd "$(dirname "$0")/.."
root="$PWD"

cargo build --release --manifest-path engine/Cargo.toml
mkdir -p dist
cp engine/target/release/engine dist/engine
echo "dist ready: $root/dist"