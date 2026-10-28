#!/usr/bin/env bash
# Package dist-app/Whisper Studio.app into a compressed DMG with an
# /Applications symlink. Run macapp/build_app.sh first (and, for a release,
# macapp/notarize.sh on the app). With SIGN_IDENTITY set to a Developer ID
# the DMG is signed too, ready for macapp/notarize.sh.
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
# APP_VERSION overrides the derived version verbatim (e.g. a feature build
# labelled 2.6.0-sonic); VERSION_SUFFIX appends to whatever was derived.
if [[ -n "${APP_VERSION:-}" ]]; then VERSION="$APP_VERSION"; fi
VERSION="${VERSION}${VERSION_SUFFIX:-}"
# DMG filename must not contain path separators.
VERSION="${VERSION//\//-}"

STAGING="$BUILD_DIR/dmg-staging"
DMG_PATH="$DIST_DIR/WhisperStudio-${VERSION}.dmg"

echo "==> Staging DMG contents"
rm -rf "$STAGING"
mkdir -p "$STAGING"
# Clone rather than copy: the bundle is several GB (a full Python runtime
# plus the speech models), and a real copy needs that much free space again
# just to stage it. APFS clones cost nothing until something is written.
# Fall back to a plain copy if the volume cannot clone.
cp -Rc "$APP_DIR" "$STAGING/" 2>/dev/null || cp -R "$APP_DIR" "$STAGING/"
ln -s /Applications "$STAGING/Applications"
# First-launch instructions for recipients of an ad-hoc build (how to clear
# the Gatekeeper warning). A Developer ID build opens without them.
SIGN_IDENTITY="${SIGN_IDENTITY:--}"
README_SRC="$MACAPP_DIR/dmg-readme.txt"
if [[ "$SIGN_IDENTITY" == "-" && -f "$README_SRC" ]]; then
    cp "$README_SRC" "$STAGING/Open me first.txt"
fi

echo "==> Creating $DMG_PATH"
hdiutil create \
    -volname "Whisper Studio" \
    -srcfolder "$STAGING" \
    -format UDZO \
    -ov \
    "$DMG_PATH"

rm -rf "$STAGING"

if [[ "$SIGN_IDENTITY" != "-" ]]; then
    echo "==> Signing the DMG ($SIGN_IDENTITY)"
    codesign --force --timestamp --sign "$SIGN_IDENTITY" "$DMG_PATH"
    codesign --verify --strict "$DMG_PATH"
fi
echo "==> Done: $DMG_PATH"
