#!/bin/bash
set -e

# Installs and starts Whisper Studio from this checkout, with the same pieces
# the Mac app bundles (macapp/build_app.sh): Python 3.13 with requirements.txt,
# the Playwright browser, llama-server, ffmpeg/ffprobe, Node and the always-on
# speech models. The app opens in your default browser. Every step either
# works or stops the script with an error; nothing is skipped with a warning.

VENV_DIR="venv"
NODE_MODULES_DIR="node_modules"
SETUP_LOG="setup.log"

# Whisper Studio supports one platform: macOS 14 or later on Apple silicon
# (arm64). Stop here, before anything is checked or installed, on any other OS,
# CPU or macOS version. An Intel Mac, and a shell running under Rosetta, report
# x86_64 and are refused. Same rule as server/infrastructure/platform_guard.py.
require_macos_arm64() {
    local os arch major
    os="$(uname -s)"
    arch="$(uname -m)"
    if [ "$os" != "Darwin" ] || [ "$arch" != "arm64" ]; then
        echo "Whisper Studio requires macOS on Apple silicon (arm64). Detected: $os/$arch." >&2
        exit 1
    fi
    major="$(sw_vers -productVersion | cut -d. -f1)"
    if [ "$major" -lt 14 ]; then
        echo "Whisper Studio requires macOS 14 or later. Detected: $(sw_vers -productVersion)." >&2
        exit 1
    fi
}
require_macos_arm64

# A source install keeps its data in this checkout. WHISPER_HOME and
# WHISPER_USER_DIR point the server at another data root (the Mac app sets
# WHISPER_HOME for its own data), so an exported value would mix the two.
refuse_foreign_data_root() {
    local var
    for var in WHISPER_HOME WHISPER_USER_DIR; do
        if [ -n "${!var}" ]; then
            echo "ERROR: $var is set (${!var}). A source install keeps its data in this checkout. Run: unset $var" >&2
            exit 1
        fi
    done
}
refuse_foreign_data_root

# The Node version lives in .nvmrc only; macapp/build_app.sh reads the same file.
if [ ! -f .nvmrc ]; then
    echo "ERROR: .nvmrc is missing. It holds the Node version; restore it with: git checkout .nvmrc" >&2
    exit 1
fi
NODE_VERSION="$(tr -d ' \n' < .nvmrc)"

# Runs a noisy install command. Output goes to $SETUP_LOG on success
# (terminal stays clean, just the one-line progress message). On
# failure, surface the failing step *and* the tail of the log on the
# terminal so the user doesn't have to know to open setup.log first.
#
# Usage: run_quiet "Installing foo" foo install --bar
run_quiet() {
    local desc="$1"
    shift
    echo "$desc (logs → $SETUP_LOG)..."
    if ! "$@" >>"$SETUP_LOG" 2>&1; then
        echo ""
        echo "ERROR: $desc failed."
        if [ -s "$SETUP_LOG" ]; then
            echo ""
            echo "Last 40 lines of $SETUP_LOG:"
            echo "────────────────────────────────────────────────────────────"
            tail -n 40 "$SETUP_LOG"
            echo "────────────────────────────────────────────────────────────"
        fi
        echo ""
        echo "Full log: $(pwd)/$SETUP_LOG"
        exit 1
    fi
}

# Prerequisites that this script cannot install. Collects every unmet item
# before exiting, so the user can fix everything in a single pass.
check_prerequisites() {
    local missing=()

    if ! command -v brew >/dev/null 2>&1; then
        missing+=("Homebrew not found. Install with:
      /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"")
    fi
    # curl: downloads and the health checks. lsof: the port search.
    command -v curl >/dev/null 2>&1 || missing+=("curl not found. It ships with macOS; check your PATH.")
    command -v lsof >/dev/null 2>&1 || missing+=("lsof not found. It ships with macOS; check your PATH.")
    # swiftc: builds the capture helper (native mic and system audio).
    xcrun --find swiftc >/dev/null 2>&1 || missing+=("Xcode Command Line Tools not found. Install with: xcode-select --install")

    if [ ${#missing[@]} -gt 0 ]; then
        echo "ERROR: prerequisites are not met."
        echo ""
        local i=1
        for item in "${missing[@]}"; do
            echo "  $i. $item"
            echo ""
            i=$((i + 1))
        done
        echo "Fix the items above, then re-run 'bash setup.sh'."
        exit 1
    fi
}

# Truncate the install log so each run starts fresh.
: > "$SETUP_LOG"

FRESH=0
# Production is the default: a bare `bash setup.sh` builds the frontend and
# serves it from the backend, the same as passing `--prod`. Pass `--dev` for the
# Vite dev server with HMR.
PROD=1
ENV_FLAG=""   # tracks an explicit --dev/--prod so passing both is rejected
# Auto-open the app in the default browser once everything is ready.
# Can also be disabled via NO_OPEN=1 env var (useful for SSH sessions
# where ``open`` would launch a phantom tab on the remote Mac's display).
NO_OPEN=${NO_OPEN:-0}
for arg in "$@"; do
    case "$arg" in
        --new)
            FRESH=1
            ;;
        --dev)
            if [ "$ENV_FLAG" = "prod" ]; then
                echo "ERROR: --dev and --prod are mutually exclusive."
                exit 1
            fi
            ENV_FLAG="dev"
            PROD=0
            ;;
        --prod)
            if [ "$ENV_FLAG" = "dev" ]; then
                echo "ERROR: --dev and --prod are mutually exclusive."
                exit 1
            fi
            ENV_FLAG="prod"
            PROD=1
            ;;
        --no-open)
            NO_OPEN=1
            ;;
        -h|--help)
            echo "Usage: bash setup.sh [--new] [--dev|--prod] [--no-open]"
            echo "  --new       Delete venv/, node_modules/, static/dist/ and vendor/ and reinstall"
            echo "              them. Also backs up pricing.json (timestamped) and removes it, so"
            echo "              rates come from pricing.example.json. config.user.json is never"
            echo "              touched."
            echo "  --dev       Development mode: run the backend + Vite dev server with HMR."
            echo "  --prod      Production mode: build the frontend and serve it from the backend"
            echo "              only (no Vite dev server). This is the default."
            echo "  --no-open   Don't auto-open the app in the default browser."
            echo "              Also honored via NO_OPEN=1 env var."
            echo ""
            echo "The only weights this script fetches are the always-on speech models the"
            echo "Mac app ships inside its bundle (language ID and the speaker encoders)."
            echo "Everything else behaves like the Mac app's first launch: transcription"
            echo "models download on your first recording, index models on your first index"
            echo "build, and chat models are installed on demand from Settings > Models > Discover."
            echo "A fresh install starts in Local mode, the Mac app's first-run default:"
            echo "nothing leaves the machine until you switch to Hybrid or Cloud in"
            echo "Settings > Model mode."
            echo ""
            echo "Default: production, builds the frontend and serves it from the backend,"
            echo "then opens the app in your default browser. Use --dev for the Vite HMR server."
            exit 0
            ;;
        *)
            echo "Unknown option: $arg"
            echo "Run 'bash setup.sh --help' for usage."
            exit 1
            ;;
    esac
done

# Check everything this script cannot install before it deletes or installs
# anything, so a missing tool never leaves a half-removed checkout behind.
check_prerequisites

if [ "$FRESH" -eq 1 ]; then
    # --new rebuilds what this script installs. config.user.json holds the
    # user's settings and is never touched. pricing.json (per-key rate
    # overrides) is backed up and removed, so rates come from
    # pricing.example.json again.
    if [ -f pricing.json ]; then
        pricing_backup="pricing.json.bak.$(date +%Y%m%d%H%M%S)"
        cp pricing.json "$pricing_backup"
        rm -f pricing.json
        echo "Backed up pricing.json to $pricing_backup and removed it (rates come from pricing.example.json)."
    fi
    for dir in "$VENV_DIR" "$NODE_MODULES_DIR" static/dist vendor; do
        if [ -d "$dir" ]; then
            echo "Removing $dir/..."
            rm -rf "$dir"
        fi
    done
fi

# ── Python ─────────────────────────────────────────────────────────────────
# The venv is built from Homebrew's Python 3.13, called by its full path (the
# Mac app ships CPython 3.13, and the user's default python3 and shell files
# are never changed). It must be an arm64 build (the mlx wheels are arm64 only)
# and its sqlite3 must load extensions, because the index stores its vectors
# with sqlite-vec. A kept venv gets the same checks on every run.
BREW_PYTHON="$(brew --prefix)/opt/python@3.13/bin/python3.13"

_python_loads_extensions() {
    "$1" - >/dev/null 2>&1 <<'PYEOF'
import sqlite3
sqlite3.connect(":memory:").enable_load_extension(True)
PYEOF
}

# $2 is the advice printed with the error.
_require_venv_python() {
    local py="$1" advice="$2"
    if ! "$py" -c 'import platform, sys; sys.exit(platform.machine() != "arm64")' >/dev/null 2>&1; then
        echo "ERROR: $py is not an arm64 (Apple silicon) Python. $advice" >&2
        exit 1
    fi
    if ! "$py" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 13))' >/dev/null 2>&1; then
        echo "ERROR: $py is not Python 3.13. $advice" >&2
        exit 1
    fi
    if ! _python_loads_extensions "$py"; then
        echo "ERROR: $py cannot load SQLite extensions, which the index needs (sqlite-vec). $advice" >&2
        exit 1
    fi
}

if [ -d "$VENV_DIR" ]; then
    _require_venv_python "$VENV_DIR/bin/python" "Delete $VENV_DIR/ and re-run: bash setup.sh"
else
    if [ ! -x "$BREW_PYTHON" ]; then
        run_quiet "Installing python@3.13 via Homebrew" brew install python@3.13
    fi
    _require_venv_python "$BREW_PYTHON" "Reinstall it with: brew reinstall python@3.13"
    echo "Creating virtual environment (python: $BREW_PYTHON)..."
    "$BREW_PYTHON" -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

# Python packages: reinstalled whenever requirements.txt changes, the same
# stamp check macapp/build_app.sh uses, so a `git pull` never leaves a venv on
# old packages.
REQ_STAMP="$VENV_DIR/.requirements.sha256"
REQ_SHA="$(shasum -a 256 requirements.txt | awk '{print $1}')"
if [ -f "$REQ_STAMP" ] && [ "$(cat "$REQ_STAMP")" = "$REQ_SHA" ]; then
    echo "Python packages match requirements.txt."
else
    run_quiet "Installing Python dependencies" pip install -r requirements.txt
    echo "$REQ_SHA" > "$REQ_STAMP"
fi

# Live-preview browser: the Playwright pip package does not bundle the browser
# binary, so fetch the Chromium build the pinned version expects. Runs every
# time, so a Playwright bump fetches its build; an existing build is reused.
run_quiet "Installing Playwright Chromium (live preview)" python -m playwright install chromium

mkdir -p data

# ── Node ───────────────────────────────────────────────────────────────────
# Node lives inside the venv (nodeenv). It builds the frontend and, at runtime,
# runs the workflow harness and the workspace's own ESLint for the assistant's
# JS/TS checks (macapp/build_app.sh bundles the same version for the app). The
# app itself never calls npm or npx.
if [ -x "$VENV_DIR/bin/node" ]; then
    if [ "$("$VENV_DIR/bin/node" --version)" != "v$NODE_VERSION" ]; then
        echo "ERROR: $VENV_DIR/bin/node is $("$VENV_DIR/bin/node" --version), .nvmrc asks for v$NODE_VERSION. Delete $VENV_DIR/ and re-run: bash setup.sh" >&2
        exit 1
    fi
else
    run_quiet "Installing Node.js $NODE_VERSION into the virtual environment" \
        nodeenv --python-virtualenv --node="$NODE_VERSION"
    source "$VENV_DIR/bin/activate"
fi

# npm packages come from package-lock.json exactly (npm ci never rewrites the
# lock file), and are reinstalled whenever the lock file changes.
LOCK_STAMP="$NODE_MODULES_DIR/.package-lock.sha256"
LOCK_SHA="$(shasum -a 256 package-lock.json | awk '{print $1}')"
if [ -f "$LOCK_STAMP" ] && [ "$(cat "$LOCK_STAMP")" = "$LOCK_SHA" ]; then
    echo "npm packages match package-lock.json."
else
    run_quiet "Installing npm dependencies" npm ci
    echo "$LOCK_SHA" > "$LOCK_STAMP"
fi

# ── Command-line tools from Homebrew ───────────────────────────────────────
# git: skill/plugin cloning and the workspace git integration. AWS CLI: the
# assistant's aws_cli tool (Bedrock credentials come from `aws configure`,
# which this script cannot do for you). ripgrep comes from requirements.txt.
if ! command -v git >/dev/null 2>&1; then
    run_quiet "Installing git via Homebrew" brew install git
fi
if ! command -v aws >/dev/null 2>&1; then
    run_quiet "Installing AWS CLI via Homebrew" brew install awscli
    echo "  Run 'aws configure' to set your access keys + Bedrock region."
fi

# ── ffmpeg / ffprobe ───────────────────────────────────────────────────────
# The same static arm64 builds the Mac app bundles (macapp/ffmpeg_pin.env),
# installed into vendor/ffmpeg and handed to the server through the same
# WHISPER_FFMPEG_PATH / WHISPER_FFPROBE_PATH variables the app sets.
. macapp/ffmpeg_pin.env
FFMPEG_HOME="vendor/ffmpeg"

install_ffmpeg_tool() {
    local tool="$1" sha="$2" tmp
    tmp="$(mktemp -d)"
    curl -fsSL --retry 3 --retry-delay 2 -o "$tmp/$tool.zip" "$FFMPEG_BASE_URL/$tool.zip" || { rm -rf "$tmp"; return 1; }
    if ! echo "$sha  $tmp/$tool.zip" | shasum -a 256 -c - >/dev/null 2>&1; then
        echo "    checksum mismatch for $tool.zip"
        rm -rf "$tmp"
        return 1
    fi
    mkdir -p "$FFMPEG_HOME"
    unzip -o -q "$tmp/$tool.zip" -d "$FFMPEG_HOME" || { rm -rf "$tmp"; return 1; }
    rm -rf "$tmp"
    chmod +x "$FFMPEG_HOME/$tool"
    "$FFMPEG_HOME/$tool" -version >/dev/null 2>&1
}

if [ "$(cat "$FFMPEG_HOME/.build" 2>/dev/null)" = "$FFMPEG_BUILD_ID" ] \
    && [ -x "$FFMPEG_HOME/ffmpeg" ] && [ -x "$FFMPEG_HOME/ffprobe" ]; then
    echo "ffmpeg $FFMPEG_BUILD_ID present at $FFMPEG_HOME."
else
    rm -rf "$FFMPEG_HOME"
    run_quiet "Installing ffmpeg $FFMPEG_BUILD_ID into $FFMPEG_HOME" install_ffmpeg_tool ffmpeg "$FFMPEG_ZIP_SHA256"
    run_quiet "Installing ffprobe $FFMPEG_BUILD_ID into $FFMPEG_HOME" install_ffmpeg_tool ffprobe "$FFPROBE_ZIP_SHA256"
    echo "$FFMPEG_BUILD_ID" > "$FFMPEG_HOME/.build"
fi

# ── Capture helper ─────────────────────────────────────────────────────────
# The Mac app's native mic and system/app audio capture, built from the same
# Swift files into a program the server starts for the page
# (server/capture_helper.py). Rebuilt only when its sources change: macOS ties
# the microphone and System Audio Recording permissions to the program, so a
# rebuild can ask again.
CAPTURE_HELPER="vendor/capture/whisper-capture"
CAPTURE_SOURCES=(macapp/helper/main.swift macapp/helper/build_helper.sh macapp/shell/NativeAudioCapture.swift macapp/shell/NativeMicCapture.swift)
CAPTURE_SHA="$(cat "${CAPTURE_SOURCES[@]}" | shasum -a 256 | awk '{print $1}')"
if [ -x "$CAPTURE_HELPER" ] && [ "$(cat "$CAPTURE_HELPER.sha256" 2>/dev/null)" = "$CAPTURE_SHA" ]; then
    echo "Capture helper is up to date."
else
    run_quiet "Building the capture helper (native mic and system audio)" \
        bash macapp/helper/build_helper.sh "$CAPTURE_HELPER"
    echo "$CAPTURE_SHA" > "$CAPTURE_HELPER.sha256"
fi

# Build frontend in production mode
if [ "$PROD" -eq 1 ]; then
    run_quiet "Building frontend" npm run build
fi

# ── First-run config seeding (Mac-app parity) ────────────────────────────────
# A checkout without config.user.json gets the first-run defaults from the same
# seed_user_config() the Mac app runs on its first launch: Local mode, with
# every weight downloading on demand. The app's first-run notice then explains
# the model modes and where to change them (Settings > Model mode). An
# existing config.user.json is never read or changed here. An old config.json
# is never read, moved or deleted, and the app shows a notice about it.
if [ -f config.json ]; then
    echo "NOTE: config.json is no longer read. Settings live in config.user.json only."
    echo "  Copy what you still need from config.json into config.user.json, then delete or rename config.json."
fi
if [ ! -f config.user.json ]; then
    run_quiet "Seeding config.user.json with the first-run defaults (Local mode)" \
        python -c "from server.infrastructure.config import seed_user_config; seed_user_config()"
    if [ ! -f config.user.json ]; then
        echo "ERROR: seeding finished but config.user.json is missing (see $SETUP_LOG)." >&2
        exit 1
    fi
fi

# ── On-device serving runtime: llama.cpp (llama-server) ─────────────────────
# The ONLY on-device chat backend. llama-server owns upstream's per-model-family
# tool-call and reasoning parsers, so a GGUF added to config.user.json gets tools and
# a thinking channel with no code change.
#
# Installed in EVERY mode, matching the Mac app: a model installed later from
# Settings > Models > Discover must serve without the user re-running setup. It
# is a small runtime, no model weights come with it.
#
# The build is the one in server/local/llama_pin.env, the same release tarball
# macapp/build_app.sh bundles, so a source install and the Mac app run the same
# binary. It goes into vendor/llama, the only place the server looks outside
# the app (a llama-server on PATH or from Homebrew is never used).
. server/local/llama_pin.env
LLAMA_HOME="vendor/llama"
LLAMA_ASSET="llama-${LLAMA_TAG}-bin-macos-arm64.tar.gz"
LLAMA_URL="https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_TAG}/${LLAMA_ASSET}"

llama_server_build() {
    [ -x "$1" ] || return 1
    # The build number, from stderr: "version: 0.5.0 (build 10350, commit
    # 7fe450e)" since llama.cpp took semantic versions, "version: 10090
    # (7347430f4)" before. A semantic version's major is never the build
    # (server/local/llama_server.py::installed_build reads it the same way).
    "$1" --version 2>&1 | sed -n \
        -e 's/.*(build[[:space:]]\{1,\}\([0-9]\{1,\}\).*/\1/p' \
        -e 's/^[[:space:]]*version:[[:space:]]*\([0-9]\{1,\}\)[[:space:]]*(.*/\1/p' | head -1
}

install_llama_server() {
    local tmp
    tmp="$(mktemp -d)"
    curl -fsSL --retry 3 --retry-delay 2 -o "$tmp/$LLAMA_ASSET" "$LLAMA_URL" || { rm -rf "$tmp"; return 1; }
    if ! echo "$LLAMA_SHA256  $tmp/$LLAMA_ASSET" | shasum -a 256 -c - >/dev/null 2>&1; then
        echo "    checksum mismatch for $LLAMA_ASSET"
        rm -rf "$tmp"
        return 1
    fi
    mkdir -p "$tmp/src"
    tar -xzf "$tmp/$LLAMA_ASSET" -C "$tmp/src" || { rm -rf "$tmp"; return 1; }
    rm -rf "$LLAMA_HOME"
    mkdir -p "$LLAMA_HOME/bin"
    # llama-server plus every dylib/metallib travel together (@rpath-relative);
    # cp -RP keeps the versioned-symlink chains the loader resolves.
    find "$tmp/src" \( -type f -o -type l \) \
        \( -name 'llama-server' -o -name '*.dylib' -o -name '*.metallib' \) \
        -exec cp -RP {} "$LLAMA_HOME/bin/" \;
    rm -rf "$tmp"
    [ "$(llama_server_build "$LLAMA_HOME/bin/llama-server")" = "${LLAMA_TAG#b}" ]
}

if [ "$(llama_server_build "$LLAMA_HOME/bin/llama-server" || true)" = "${LLAMA_TAG#b}" ]; then
    echo "llama-server $LLAMA_TAG present at $LLAMA_HOME/bin/llama-server."
else
    run_quiet "Installing llama.cpp $LLAMA_TAG (llama-server) into $LLAMA_HOME" install_llama_server
fi

# ── Model weights: only the always-on speech models are fetched ─────────────
# Language ID (VoxLingua107) and the two speaker encoders (ReDimNet2 and ECAPA)
# are bundled infrastructure (~190MB total): every transcription path needs
# them, they are hidden from the Settings model lists, and the Mac app ships
# them inside the bundle (build_app.sh stages the same three and stops when one
# fails). Idempotent: the ensure functions check their sentinel files and
# return at once when the models are already on disk.
run_quiet "Fetching the always-on speech models (language ID + speaker encoders, ~190MB)" \
    python -c "
from server.asr.lid import _ensure_model
from server.diarization.embedder import _ensure_ecapa_files, _ensure_redimnet_files
_ensure_model()
_ensure_ecapa_files()
_ensure_redimnet_files()
"
# Everything else downloads ON DEMAND with an in-app progress banner, into
# ./models (idempotent, resumable):
#   * transcription engines, on your first recording (or pre-pull them from
#     Settings > Models > Transcription),
#   * on-device index weights (Qwen3 embed/rerank, GLiNER), on your first index
#     build in hybrid/local mode,
#   * on-device chat models, installed from Settings > Models > Discover
#     (the app ships with none).
echo ""
echo "Other model weights download on demand: transcription engines on your"
echo "  first recording, index models on your first index build, and chat"
echo "  models from Settings > Models > Discover."

echo ""
echo "Setup complete. Starting server..."

# Fixed ports, so the browser keeps its per-origin storage (theme, layout,
# mic permission) between runs. The source install uses 41430 (backend) and
# 41460 (Vite); the Mac app uses 41410, so the two never compete. When the
# preferred port is busy, try the nearest free one (+1, -1, +2, -2 ... up to
# 5 each way) and stop with an error if all 11 are taken. Never a random port.
pick_port() {
    local preferred="$1" step
    if ! lsof -nP -iTCP:"$preferred" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "$preferred"; return 0
    fi
    for step in 1 2 3 4 5; do
        for candidate in $((preferred + step)) $((preferred - step)); do
            if ! lsof -nP -iTCP:"$candidate" -sTCP:LISTEN >/dev/null 2>&1; then
                echo "$candidate"; return 0
            fi
        done
    done
    echo "ERROR: ports $((preferred - 5)) to $((preferred + 5)) are all in use. Free one with: lsof -nP -iTCP:$preferred -sTCP:LISTEN" >&2
    return 1
}

PORT=$(pick_port 41430)
export PORT
# Dev mode: the Vite port is chosen before the backend starts, so the backend
# knows it. Vite proxies /api to the backend, and a confined command must be
# denied that port as well as the backend's own (server/sandbox.py).
if [ "$PROD" -eq 0 ]; then
    VITE_PORT=$(pick_port 41460)
    export WHISPER_DEV_PROXY_PORT="$VITE_PORT"
fi

# The binaries the server spawns, pinned the way the Mac app pins its bundled
# copies (macapp/shell/main.swift). llama-server is found in vendor/llama.
export WHISPER_FFMPEG_PATH="$PWD/$FFMPEG_HOME/ffmpeg"
export WHISPER_FFPROBE_PATH="$PWD/$FFMPEG_HOME/ffprobe"
export WHISPER_NODE_PATH="$PWD/$VENV_DIR/bin/node"

# Only the packaged macOS shell owns the backend parent-watchdog contract.
# A dev server launched from an app terminal can inherit its outer shell's
# WHISPER_PARENT_PID; passing that stale PID to this new backend makes it exit
# as soon as startup completes. setup.sh is the direct parent here, so clear it.
unset WHISPER_PARENT_PID

SERVER_PID=""
VITE_PID=""
stop_children() {
    [ -n "$VITE_PID" ] && kill "$VITE_PID" 2>/dev/null
    [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null
    return 0
}
# Registered before anything is spawned, so every exit path stops the children.
trap stop_children EXIT

python -m server.main &
SERVER_PID=$!

# Wait for the backend to be ready before starting Vite. Startup no longer
# loads any model (weights download and load on demand), but a cold first boot
# still compiles bytecode and opens the databases, so keep a generous ceiling.
echo "Waiting for backend (up to 90s on a cold first boot)..."
backend_ready=0
for i in $(seq 1 90); do
    if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
        echo "Backend ready after ${i}s."
        backend_ready=1
        break
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        break
    fi
    sleep 1
done
if [ "$backend_ready" -ne 1 ]; then
    echo "ERROR: the backend did not answer on http://127.0.0.1:$PORT/health. Its output is above." >&2
    exit 1
fi

if [ "$PROD" -eq 0 ]; then
    # Forward the chosen backend port to vite so its proxy targets the
    # right server when the backend lands next to 41430. A separate var
    # (not ``PORT``) because vite reads ``PORT`` as its own listen port,
    # which would clash with ``--port``.
    BACKEND_PORT="$PORT" npx vite --port "$VITE_PORT" --strictPort &
    VITE_PID=$!
fi

# Determine the URL the user should actually visit.
#   - prod: backend serves the SPA bundle on its own port
#   - dev:  Vite serves the SPA on its port and proxies /api + /ws to the
#           backend; the user opens Vite, not the backend
if [ "$PROD" -eq 0 ]; then
    APP_URL="http://127.0.0.1:$VITE_PORT"
else
    APP_URL="http://127.0.0.1:$PORT"
fi

# In dev mode, Vite has just been spawned in the background, so wait for
# it to actually start serving before we tell the browser to open it.
# Otherwise the browser races Vite and shows "site can't be reached".
# 15s ceiling is generous; Vite typically responds within ~2s.
if [ "$PROD" -eq 0 ]; then
    echo "Waiting for Vite dev server..."
    vite_ready=0
    for i in $(seq 1 30); do
        if curl -sf "$APP_URL" >/dev/null 2>&1; then
            vite_ready=1
            break
        fi
        sleep 0.5
    done
    if [ "$vite_ready" -ne 1 ]; then
        echo "ERROR: Vite did not answer on $APP_URL within 15s. Its output is above." >&2
        exit 1
    fi
fi

echo ""
if [ "$PROD" -eq 0 ]; then
    echo "Whisper Studio is running at $APP_URL"
    echo ""
    echo "  Frontend (Vite + HMR) → $APP_URL"
    echo "  Backend (FastAPI)     → http://127.0.0.1:$PORT"
else
    echo "Whisper Studio is running at $APP_URL"
fi
echo ""
echo "In the browser, Apple translation is not available (it needs the Mac app)."
echo "The first recording from the mic or from \"This Mac\" makes macOS ask for"
echo "permission for this terminal app. Click Allow."
echo ""

# Auto-open the right URL in the default browser.
if [ "$NO_OPEN" -eq 0 ]; then
    echo "Opening $APP_URL in your default browser..."
    open "$APP_URL"
fi

echo "Press Ctrl+C to stop."

wait "$SERVER_PID"
