#!/usr/bin/env bash
# Whisper Studio — macOS app bundle assembler.
#
# Builds dist-app/Whisper Studio.app: frontend, standalone Python runtime,
# llama-server, ffmpeg/ffprobe, node, the compiled Swift shell, and signs
# everything. Idempotent: downloads are cached in build-app/downloads/ and
# completed stages are skipped on re-runs.
#
# Usage:
#   bash macapp/build_app.sh
#   SIGN_IDENTITY="Developer ID Application: ..." bash macapp/build_app.sh
#
# Apple Silicon (arm64) only, macOS 14+.
set -euo pipefail

# ---------------------------------------------------------------------------
# Pinned versions and checksums (all URLs verified 2026-08-03)
# ---------------------------------------------------------------------------

# python-build-standalone (astral-sh), CPython 3.13, arm64 macOS, stripped.
PYTHON_RELEASE_TAG="20260728"
PYTHON_VERSION="3.13.14"
PYTHON_TARBALL="cpython-${PYTHON_VERSION}+${PYTHON_RELEASE_TAG}-aarch64-apple-darwin-install_only_stripped.tar.gz"
PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_RELEASE_TAG}/${PYTHON_TARBALL}"
PYTHON_SHA256="aa2a054f5e04bde63ae199e3bb6bbb634e457423efd294842deeb1299e7e5932"

# llama.cpp official release binaries (>= b10090 required by the backend).
LLAMA_TAG="b10243"
LLAMA_ASSET="llama-${LLAMA_TAG}-bin-macos-arm64.tar.gz"
LLAMA_URL="https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_TAG}/${LLAMA_ASSET}"

# ffmpeg/ffprobe static arm64 release builds (ffmpeg 8.1.2) from
# https://ffmpeg.martin-riedl.de (per-binary zips).
FFMPEG_BUILD_ID="1783011502_8.1.2"
FFMPEG_BASE_URL="https://ffmpeg.martin-riedl.de/download/macos/arm64/${FFMPEG_BUILD_ID}"
FFMPEG_ZIP_URL="${FFMPEG_BASE_URL}/ffmpeg.zip"
FFPROBE_ZIP_URL="${FFMPEG_BASE_URL}/ffprobe.zip"
FFMPEG_ZIP_SHA256="ef1aa60006c7b77ce170c1608c08d8e4ba1c30c5746f2ac986ded932d0ac2c3c"
FFPROBE_ZIP_SHA256="c39787f4af7a3932502d2d48db6f6feaaa836b48a73ef78c32cc3285df61dfaf"

# Node.js official arm64 macOS build (only bin/node is bundled).
NODE_VERSION="v22.16.0"
NODE_TARBALL="node-${NODE_VERSION}-darwin-arm64.tar.gz"
NODE_URL="https://nodejs.org/dist/${NODE_VERSION}/${NODE_TARBALL}"
NODE_SHA256="1d7f34ec4c03e12d8b33481e5c4560432d7dc31a0ef3ff5a4d9a8ada7cf6ecc9"

# Signing: "-" = ad-hoc (default). Set SIGN_IDENTITY to a Developer ID
# Application identity for a distributable build.
SIGN_IDENTITY="${SIGN_IDENTITY:--}"
BUNDLE_ID="${BUNDLE_ID:-io.paukode.whisper-studio}"

# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

MACAPP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$MACAPP_DIR/.." && pwd)"
BUILD_DIR="$REPO_ROOT/build-app"
DL_DIR="$BUILD_DIR/downloads"
DIST_DIR="$REPO_ROOT/dist-app"

APP_DIR="$DIST_DIR/Whisper Studio.app"
CONTENTS_DIR="$APP_DIR/Contents"
RES_DIR="$CONTENTS_DIR/Resources"

mkdir -p "$DL_DIR" "$DIST_DIR"

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
substep() { printf '    %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# verify_sha <file> <sha256>
verify_sha() {
    echo "$2  $1" | shasum -a 256 -c - >/dev/null 2>&1
}

# download <url> <dest> [sha256]
# Cached: skips the download when dest exists (and matches the checksum, if
# one is given). A checksum mismatch on a cached file forces a re-download.
download() {
    local url="$1" dest="$2" sha="${3:-}"
    if [[ -f "$dest" ]]; then
        if [[ -z "$sha" ]] || verify_sha "$dest" "$sha"; then
            substep "cached: $(basename "$dest")"
            return 0
        fi
        substep "cached file failed checksum, re-downloading: $(basename "$dest")"
        rm -f "$dest"
    fi
    substep "downloading $(basename "$dest")"
    curl -fL --retry 3 --retry-delay 2 -o "$dest.part" "$url"
    if [[ -n "$sha" ]] && ! verify_sha "$dest.part" "$sha"; then
        rm -f "$dest.part"
        die "checksum mismatch for $url"
    fi
    mv "$dest.part" "$dest"
}

# is_macho <file>: true when the file starts with a Mach-O or fat magic.
is_macho() {
    local magic
    magic="$(xxd -p -l 4 "$1" 2>/dev/null || true)"
    case "$magic" in
        cffaedfe|cefaedfe|feedfacf|feedface|cafebabe|bebafeca) return 0 ;;
        *) return 1 ;;
    esac
}

[[ "$(uname -s)" == "Darwin" ]] || die "this script only runs on macOS"
[[ "$(uname -m)" == "arm64" ]] || die "Apple Silicon (arm64) only"
command -v curl >/dev/null || die "curl not found"
command -v rsync >/dev/null || die "rsync not found"
command -v codesign >/dev/null || die "codesign not found (install Command Line Tools)"

# Version, same rule as make_dmg.sh: git describe, else 0.1.0.
VERSION="$(git -C "$REPO_ROOT" describe --tags --always 2>/dev/null || echo "0.1.0")"
VERSION="${VERSION#v}"
[[ -n "$VERSION" ]] || VERSION="0.1.0"

log "Building Whisper Studio.app (version $VERSION, identity: $SIGN_IDENTITY)"

# ---------------------------------------------------------------------------
# Stage a: frontend
# ---------------------------------------------------------------------------

log "[a] Frontend build (npm run build)"
[[ -d "$REPO_ROOT/node_modules" ]] \
    || die "node_modules missing at $REPO_ROOT — run 'npm ci' (or npm install) first"
(cd "$REPO_ROOT" && npm run build)
[[ -f "$REPO_ROOT/static/dist/index.html" ]] \
    || die "frontend build produced no static/dist/index.html"

# ---------------------------------------------------------------------------
# Stage b: standalone Python runtime + backend dependencies
# ---------------------------------------------------------------------------

log "[b] Python runtime ($PYTHON_TARBALL)"
PY_DIR="$BUILD_DIR/python"
PY_BIN="$PY_DIR/bin/python3"
download "$PYTHON_URL" "$DL_DIR/$PYTHON_TARBALL" "$PYTHON_SHA256"
if [[ -x "$PY_BIN" ]]; then
    substep "runtime already extracted: $PY_DIR"
else
    rm -rf "$PY_DIR"
    tar -xzf "$DL_DIR/$PYTHON_TARBALL" -C "$BUILD_DIR"   # tarball root is python/
    [[ -x "$PY_BIN" ]] || die "python3 missing after extracting $PYTHON_TARBALL"
fi

REQ_STAMP="$PY_DIR/.requirements.sha256"
REQ_SHA="$(shasum -a 256 "$REPO_ROOT/requirements.txt" | awk '{print $1}')"
if [[ -f "$REQ_STAMP" && "$(cat "$REQ_STAMP")" == "$REQ_SHA" ]]; then
    substep "requirements already installed (stamp matches)"
else
    substep "pip install -r requirements.txt"
    "$PY_BIN" -m pip install -r "$REPO_ROOT/requirements.txt" --no-warn-script-location
    echo "$REQ_SHA" > "$REQ_STAMP"
fi

# ---------------------------------------------------------------------------
# Stage c: llama-server
# ---------------------------------------------------------------------------

log "[c] llama-server ($LLAMA_TAG)"
LLAMA_DIR="$BUILD_DIR/llama"
LLAMA_BIN_DIR="$LLAMA_DIR/bin"
download "$LLAMA_URL" "$DL_DIR/$LLAMA_ASSET"
if [[ -x "$LLAMA_BIN_DIR/llama-server" ]]; then
    substep "already extracted: $LLAMA_BIN_DIR"
else
    rm -rf "$LLAMA_DIR"
    mkdir -p "$LLAMA_DIR/src" "$LLAMA_BIN_DIR"
    tar -xzf "$DL_DIR/$LLAMA_ASSET" -C "$LLAMA_DIR/src"
    # llama-server plus every dylib/metallib travel together (@rpath-relative).
    # cp -RP keeps the versioned-symlink chains (libggml.dylib -> libggml.0.dylib
    # -> libggml.0.18.0.dylib) that the loader resolves; a plain -type f copy
    # drops them and llama-server fails at dyld time.
    find "$LLAMA_DIR/src" \( -type f -o -type l \) \
        \( -name 'llama-server' -o -name '*.dylib' -o -name '*.metallib' \) \
        -exec cp -RP {} "$LLAMA_BIN_DIR/" \;
    [[ -f "$LLAMA_BIN_DIR/llama-server" ]] \
        || die "llama-server not found inside $LLAMA_ASSET"
    chmod +x "$LLAMA_BIN_DIR/llama-server"
    rm -rf "$LLAMA_DIR/src"
fi

# ---------------------------------------------------------------------------
# Stage d: ffmpeg / ffprobe (static arm64 release builds)
# ---------------------------------------------------------------------------

log "[d] ffmpeg + ffprobe ($FFMPEG_BUILD_ID)"
FFMPEG_DIR="$BUILD_DIR/ffmpeg"
mkdir -p "$FFMPEG_DIR"
download "$FFMPEG_ZIP_URL" "$DL_DIR/ffmpeg-$FFMPEG_BUILD_ID.zip" "$FFMPEG_ZIP_SHA256"
download "$FFPROBE_ZIP_URL" "$DL_DIR/ffprobe-$FFMPEG_BUILD_ID.zip" "$FFPROBE_ZIP_SHA256"
for tool in ffmpeg ffprobe; do
    if [[ ! -x "$FFMPEG_DIR/$tool" ]]; then
        unzip -o -q "$DL_DIR/$tool-$FFMPEG_BUILD_ID.zip" -d "$FFMPEG_DIR"
        [[ -f "$FFMPEG_DIR/$tool" ]] || die "$tool missing after unzip"
        chmod +x "$FFMPEG_DIR/$tool"
    fi
    file "$FFMPEG_DIR/$tool" | grep -q 'Mach-O.*arm64' \
        || die "$tool is not an arm64 Mach-O binary"
    "$FFMPEG_DIR/$tool" -version >/dev/null 2>&1 \
        || die "$tool failed to run ($tool -version)"
    substep "$tool ok ($("$FFMPEG_DIR/$tool" -version 2>/dev/null | head -1 | cut -d' ' -f1-3))"
done

# ---------------------------------------------------------------------------
# Stage e: node (bin/node only)
# ---------------------------------------------------------------------------

log "[e] node ($NODE_VERSION)"
NODE_DIR="$BUILD_DIR/node"
download "$NODE_URL" "$DL_DIR/$NODE_TARBALL" "$NODE_SHA256"
if [[ -x "$NODE_DIR/node" ]]; then
    substep "already extracted: $NODE_DIR/node"
else
    rm -rf "$NODE_DIR"
    mkdir -p "$NODE_DIR"
    tar -xzf "$DL_DIR/$NODE_TARBALL" -C "$NODE_DIR" --strip-components=2 \
        "node-${NODE_VERSION}-darwin-arm64/bin/node"
    [[ -x "$NODE_DIR/node" ]] || die "node missing after extraction"
fi

# ---------------------------------------------------------------------------
# Stage f: assemble the .app bundle
# ---------------------------------------------------------------------------

log "[f] Assembling $APP_DIR"

# f.0: compile the Swift shell (fast; always rebuild for freshness).
bash "$MACAPP_DIR/shell/build_shell.sh"

rm -rf "$APP_DIR"
mkdir -p "$CONTENTS_DIR/MacOS" "$RES_DIR/bin"

# f.1: Info.plist from the template.
sed -e "s|@@BUNDLE_ID@@|$BUNDLE_ID|g" -e "s|@@VERSION@@|$VERSION|g" \
    "$MACAPP_DIR/Info.plist" > "$CONTENTS_DIR/Info.plist"
plutil -lint -s "$CONTENTS_DIR/Info.plist" || die "generated Info.plist is invalid"

# f.2: shell binary.
cp "$BUILD_DIR/shell/WhisperStudio" "$CONTENTS_DIR/MacOS/WhisperStudio"

# f.3: placeholder icon (solid colour, generated with the bundled Python —
# pure stdlib PNG writer, then sips + iconutil).
ICON_DIR="$BUILD_DIR/icon"
if [[ ! -f "$ICON_DIR/AppIcon.icns" ]]; then
    substep "generating placeholder AppIcon.icns"
    rm -rf "$ICON_DIR"
    mkdir -p "$ICON_DIR/AppIcon.iconset"
    "$PY_BIN" - "$ICON_DIR/base.png" <<'PYEOF'
import struct, sys, zlib
size = 1024
r, g, b = 0x4B, 0x3F, 0x8F  # muted violet placeholder
row = bytes([0]) + bytes([r, g, b, 255]) * size
raw = row * size
def chunk(tag, data):
    out = struct.pack(">I", len(data)) + tag + data
    return out + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
png = b"\x89PNG\r\n\x1a\n"
png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
png += chunk(b"IDAT", zlib.compress(raw))
png += chunk(b"IEND", b"")
open(sys.argv[1], "wb").write(png)
PYEOF
    for s in 16 32 128 256 512; do
        sips -z "$s" "$s" "$ICON_DIR/base.png" \
            --out "$ICON_DIR/AppIcon.iconset/icon_${s}x${s}.png" >/dev/null
        d=$((s * 2))
        sips -z "$d" "$d" "$ICON_DIR/base.png" \
            --out "$ICON_DIR/AppIcon.iconset/icon_${s}x${s}@2x.png" >/dev/null
    done
    iconutil -c icns "$ICON_DIR/AppIcon.iconset" -o "$ICON_DIR/AppIcon.icns"
fi
cp "$ICON_DIR/AppIcon.icns" "$RES_DIR/AppIcon.icns"

# f.4: Python runtime.
substep "copying python runtime"
rsync -a --delete "$PY_DIR/" "$RES_DIR/python/"
rm -f "$RES_DIR/python/.requirements.sha256"

# f.5: backend tree.
substep "copying backend (server/, static/, skills/, plugins/, configs)"
mkdir -p "$RES_DIR/backend"
rsync -a --delete \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude '.git' \
    --exclude 'tests/' \
    --exclude 'venv/' \
    --exclude 'node_modules' \
    --exclude 'models/' \
    --exclude 'storage/' \
    --exclude 'data/' \
    "$REPO_ROOT/server" \
    "$REPO_ROOT/static" \
    "$REPO_ROOT/skills" \
    "$REPO_ROOT/plugins" \
    "$RES_DIR/backend/"
cp "$REPO_ROOT/config.example.json" \
   "$REPO_ROOT/pricing.example.json" \
   "$REPO_ROOT/PROMPT_RULES.md" \
   "$RES_DIR/backend/"

# f.6: helper binaries.
substep "copying bin/ (llama-server + dylibs/metallib, ffmpeg, ffprobe, node)"
cp "$LLAMA_BIN_DIR"/* "$RES_DIR/bin/"
cp "$FFMPEG_DIR/ffmpeg" "$FFMPEG_DIR/ffprobe" "$NODE_DIR/node" "$RES_DIR/bin/"
chmod +x "$RES_DIR/bin/llama-server" "$RES_DIR/bin/ffmpeg" \
         "$RES_DIR/bin/ffprobe" "$RES_DIR/bin/node"

# ---------------------------------------------------------------------------
# Stage g: code signing
# ---------------------------------------------------------------------------

log "[g] Signing (identity: $SIGN_IDENTITY)"
SIGN_ARGS=(--force --sign "$SIGN_IDENTITY")
if [[ "$SIGN_IDENTITY" != "-" ]]; then
    # Hardened runtime + timestamp + entitlements only make sense with a real
    # identity; combining --options runtime with ad-hoc signing breaks launch.
    SIGN_ARGS+=(--options runtime --timestamp
                --entitlements "$MACAPP_DIR/entitlements.plist")
fi

substep "signing Mach-O files under Resources/ (this can take a while)"
SIGNED=0
while IFS= read -r -d '' f; do
    if is_macho "$f"; then
        codesign "${SIGN_ARGS[@]}" "$f" 2>/dev/null \
            || codesign "${SIGN_ARGS[@]}" "$f"
        SIGNED=$((SIGNED + 1))
    fi
done < <(find "$RES_DIR" -type f -print0)
substep "signed $SIGNED Mach-O files"

substep "signing main executable"
codesign "${SIGN_ARGS[@]}" "$CONTENTS_DIR/MacOS/WhisperStudio"

substep "signing app bundle"
codesign "${SIGN_ARGS[@]}" "$APP_DIR"

codesign --verify "$APP_DIR" || die "codesign verification failed"

log "Done: $APP_DIR"
substep "launch:      open \"$APP_DIR\""
substep "backend log: ~/Library/Application Support/WhisperStudio/logs/backend.log"
substep "make a DMG:  bash macapp/make_dmg.sh"
