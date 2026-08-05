#!/usr/bin/env bash
# Compile the Whisper Studio native shell (all macapp/shell/*.swift, no
# xcodeproj). Output: build-app/shell/WhisperStudio
#
# smoke_list_sources.swift is compiled too but its whole body is behind
# `#if SMOKE_CLI`, so it contributes nothing to the app binary. To build the
# enumeration smoke CLI instead, add -D SMOKE_CLI and drop main.swift (see the
# header of smoke_list_sources.swift).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OUT_DIR="$REPO_ROOT/build-app/shell"

mkdir -p "$OUT_DIR"

echo "==> Compiling macapp/shell/*.swift (swiftc, arm64-apple-macos14)"
swiftc -O -target arm64-apple-macos14 \
    "$SCRIPT_DIR"/*.swift \
    -o "$OUT_DIR/WhisperStudio" \
    -framework AppKit -framework WebKit \
    -framework CoreAudio -framework AudioToolbox -framework AVFoundation

echo "==> Shell binary: $OUT_DIR/WhisperStudio"
