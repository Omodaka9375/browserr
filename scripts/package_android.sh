#!/usr/bin/env bash
# Package the android-arm64 release tarball. Runs on a Linux host with
# the Android NDK r30: cross-builds the engine for aarch64 (API 29),
# then archives the flat payload exactly like package.sh (engine at the
# archive root next to driver.py etc). NDK location comes from
# ANDROID_NDK_HOME (CI sets it); there is no host dependency on the
# paths hardcoded in .cargo/config.toml because [env] values lose to
# real environment variables.
# Output: browserr-<version>-android-arm64.tar.gz
#   - one engine binary for every Android device: ARM64 with 64-bit
#     userland; 32-bit ARM and x86 devices are not supported.
#   - headless only: no headed mode, no audio.
set -eu
cd "$(dirname "$0")/.."
root="$PWD"

target="aarch64-linux-android"
ndk="${ANDROID_NDK_HOME:?ANDROID_NDK_HOME must point at NDK r30}"
toolchain="$ndk/toolchains/llvm/prebuilt/linux-x86_64"
export ANDROID_NDK_HOME="$ndk"
export PATH="$toolchain/bin:$PATH"

# Cross-build (release). .cargo/config.toml sets linker/ar per target,
# but with Linux toolchain paths (its committed values point at the
# Windows dev box); the [env] CC/CXX blocks lose to real env vars, so
# we set the full toolchain environment here.
export CC_aarch64_linux_android="$toolchain/bin/clang"
export CXX_aarch64_linux_android="$toolchain/bin/clang++"
export AR_aarch64_linux_android="$toolchain/bin/llvm-ar"
export RANLIB_aarch64_linux_android="$toolchain/bin/llvm-ranlib"
export CFLAGS_aarch64_linux_android="--target=aarch64-linux-android29 --sysroot=$toolchain/sysroot"
export CXXFLAGS_aarch64_linux_android="--target=aarch64-linux-android29 --sysroot=$toolchain/sysroot"
export BINDGEN_EXTRA_CLANG_ARGS_aarch64_linux_android="--target=aarch64-linux-android29 --sysroot=$toolchain/sysroot -isystem $toolchain/sysroot/usr/include/c++/v1"
# .cargo/config.toml linkers point at windows-x86_64 NDK paths; override
# with the linux toolchain through cargo config on the command line.
mkdir -p .cargo-android
cat > .cargo-android/config.toml <<EOF
[target.aarch64-linux-android]
linker = "$toolchain/bin/aarch64-linux-android29-clang"
ar = "$toolchain/bin/llvm-ar"
rustflags = ["-C", "link-arg=-landroid"]
EOF

cargo build --release --manifest-path engine/Cargo.toml \
    --locked --target "$target" \
    --config "build.target-dir=\"$root/engine/target\"" \
    --config ".cargo=\"$root/.cargo-android\""

if [ -n "${RELEASE_VERSION:-}" ]; then
    version="${RELEASE_VERSION#v}"
else
    version=$(grep -m1 '^version' engine/Cargo.toml | sed 's/.*"\(.*\)".*/\1/')
fi
platform="android-arm64"
out="browserr-$version-$platform.tar.gz"
rm -f "$out"
stage="$(mktemp -d)"
cp "$root/engine/target/$target/release/engine" \
   "$root/tools/driver.py" \
   "$root/tools/browserr_mcp.py" \
   "$root/README.md" \
   "$root/LICENSE" \
   "$stage/"
chmod +x "$stage/engine"
# Flat unpack layout, same as package.sh: engine at the root, driver.py
# finds it next to itself.
tar -czf "$out" -C "$stage" .
rm -rf "$stage"
echo "packaged: $out ($(du -h "$out" | cut -f1))"
