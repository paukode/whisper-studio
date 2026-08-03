#!/usr/bin/env bash
# Compile the Whisper Studio native shell (single Swift file, no xcodeproj).
# Output: build-app/shell/WhisperStudio
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OUT_DIR="$REPO_ROOT/build-app/shell"

mkdir -p "$OUT_DIR"

echo "==> Compiling macapp/shell/main.swift (swiftc, arm64-apple-macos14)"
swiftc -O -target arm64-apple-macos14 \
    "$SCRIPT_DIR/main.swift" \
    -o "$OUT_DIR/WhisperStudio" \
    -framework AppKit -framework WebKit

echo "==> Shell binary: $OUT_DIR/WhisperStudio"
