#!/bin/bash
# WSL engine build. Run via: wsl -d Ubuntu -u root bash /mnt/d/.../wsl_build.sh
set -e
export PATH="/root/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export CARGO_HOME=/root/.cargo
cd /mnt/d/Code/2026/Browserr/engine
cargo build --release 2>&1 | tail -15
echo "EXIT:$?"
