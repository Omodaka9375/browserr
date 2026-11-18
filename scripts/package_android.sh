#!/usr/bin/env bash
# Package the android-arm64 release tarball. Runs on a Linux host with
# the Android NDK r30: cross-builds the engine for aarch64 (API 29),
# then archives the flat payload exactly like package.sh (engine at the
# archive root next to driver.py etc). NDK location comes from
# ANDROID_NDK_HOME (CI sets it).
#
# The committed .cargo/config.toml carries Windows-local NDK paths (the
# dev box), so every toolchain setting is overridden here via --config,
# which outranks project config files. Real environment variables also
# outrank the config's [env] table, so the CC/CXX exports below win.
#
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

export CC_aarch64_linux_android="$toolchain/bin/clang"
export CXX_aarch64_linux_android="$toolchain/bin/clang++"
export AR_aarch64_linux_android="$toolchain/bin/llvm-ar"
export RANLIB_aarch64_linux_android="$toolchain/bin/llvm-ranlib"
export CFLAGS_aarch64_linux_android="--target=aarch64-linux-android29 --sysroot=$toolchain/sysroot"
export CXXFLAGS_aarch64_linux_android="--target=aarch64-linux-android29 --sysroot=$toolchain/sysroot"
export BINDGEN_EXTRA_CLANG_ARGS_aarch64_linux_android="--target=aarch64-linux-android29 --sysroot=$toolchain/sysroot -isystem $toolchain/sysroot/usr/include/c++/v1"

cargo build --release --manifest-path engine/Cargo.toml \
    --locked --target "$target" \
    --config "build.target-dir=\"$root/engine/target\"" \
    --config "target.aarch64-linux-android.linker=\"$toolchain/bin/aarch64-linux-android29-clang\"" \
    --config "target.aarch64-linux-android.ar=\"$toolchain/bin/llvm-ar\"" \
    --config 'target.aarch64-linux-android.rustflags=["-C", "link-arg=-landroid"]'

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
   "$toolchain/sysroot/usr/lib/aarch64-linux-android/libc++_shared.so" \
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
