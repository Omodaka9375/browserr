#!/bin/bash
# Verify the Linux engine build: boot, navigate, native PNG/Sixel frames.
set -e
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export BROWSERR_ENGINE=/mnt/d/Code/2026/Browserr/engine/target/release/engine
export HOME=/root
cd /mnt/d/Code/2026/Browserr
python3 term/browserr_term.py --self-test file:///mnt/d/Code/2026/Browserr/term/native-fixture.html
echo "WSL-VERIFY-OK"
