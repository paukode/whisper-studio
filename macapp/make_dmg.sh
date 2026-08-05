#!/usr/bin/env bash
# Package dist-app/Whisper Studio.app into a compressed DMG with an
# /Applications symlink. Run macapp/build_app.sh first.
set -euo pipefail

MACAPP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$MACAPP_DIR/.." && pwd)"
BUILD_DIR="$REPO_ROOT/build-app"
DIST_DIR="$REPO_ROOT/dist-app"
APP_DIR="$DIST_DIR/Whisper Studio.app"

[[ -d "$APP_DIR" ]] || { echo "ERROR: $APP_DIR not found — run macapp/build_app.sh first" >&2; exit 1; }

# Version, same rule as build_app.sh: git describe, else 0.1.0.
VERSION="$(git -C "$REPO_ROOT" describe --tags --always 2>/dev/null || echo "0.1.0")"
VERSION="${VERSION#v}"
[[ -n "$VERSION" ]] || VERSION="0.1.0"
# DMG filename must not contain path separators.
VERSION="${VERSION//\//-}"

STAGING="$BUILD_DIR/dmg-staging"
DMG_PATH="$DIST_DIR/WhisperStudio-${VERSION}.dmg"

echo "==> Staging DMG contents"
rm -rf "$STAGING"
mkdir -p "$STAGING"
cp -R "$APP_DIR" "$STAGING/"
ln -s /Applications "$STAGING/Applications"
# First-launch instructions for recipients of an ad-hoc / un-notarized build
# (how to clear the Gatekeeper warning). Shipped inside the disk image.
README_SRC="$MACAPP_DIR/dmg-readme.txt"
[[ -f "$README_SRC" ]] && cp "$README_SRC" "$STAGING/Open me first.txt"

echo "==> Creating $DMG_PATH"
hdiutil create \
    -volname "Whisper Studio" \
    -srcfolder "$STAGING" \
    -format UDZO \
    -ov \
    "$DMG_PATH"

rm -rf "$STAGING"
echo "==> Done: $DMG_PATH"
