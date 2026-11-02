#!/bin/bash
# Compiles the capture helper (macapp/helper/main.swift plus the app's own
# capture files) for a source install. Usage: build_helper.sh <output path>
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="$1"
mkdir -p "$(dirname "$out")"
swiftc -O -target arm64-apple-macos14.4 \
    "$here/../shell/NativeAudioCapture.swift" \
    "$here/../shell/NativeMicCapture.swift" \
    "$here/main.swift" \
    -o "$out"
