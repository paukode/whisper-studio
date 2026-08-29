#!/bin/bash
set -e

VENV_DIR="venv"
NODE_MODULES_DIR="node_modules"
SETUP_LOG="setup.log"
# Read Node version from .nvmrc if present, otherwise fall back to a known-good
# default. .nvmrc lets us bump Node without editing this script.
NODE_VERSION="$( [ -f .nvmrc ] && tr -d ' \n' < .nvmrc || echo 22.16.0 )"

# Ensure Python 3.10+ (the backend uses match statements, asyncio.TaskGroup,
# and `dict[str, dict]` syntax, none of which work below 3.10). If the active
# Python is older or missing, offer to install Python 3.12 via Homebrew, persist
# it on PATH in ~/.zshrc, and use it for the rest of this run.
_py_minor_ok() {
    # Args: <major> <minor>. True when version >= 3.10 (or major > 3).
    [ "$1" -gt 3 ] || { [ "$1" -eq 3 ] && [ "$2" -ge 10 ]; }
}

_install_python_312() {
    # Need an interactive terminal to ask, and Homebrew to install.
    if [ ! -t 0 ]; then
        echo "Re-run in an interactive terminal to auto-install, or install Python 3.10+ yourself. Exiting."
        exit 1
    fi
    if ! command -v brew >/dev/null 2>&1; then
        echo "Homebrew is needed to install Python automatically. Get it at https://brew.sh, then re-run. Exiting."
        exit 1
    fi

    printf "Install Python 3.12 via Homebrew now? [y/N] "
    read -r reply
    case "$reply" in
        [Yy] | [Yy][Ee][Ss]) ;;
        *) echo "Cannot continue without Python 3.10 or newer. Exiting."; exit 1 ;;
    esac

    echo "Installing Python 3.12 via Homebrew (this can take a few minutes)..."
    brew install python@3.12

    # python@3.12 is keg-only; its libexec/bin holds the unversioned python3/pip3.
    local py_bin
    py_bin="$(brew --prefix python@3.12)/libexec/bin"

    # Persist for future shells so `python3` is 3.12 going forward.
    local zrc="$HOME/.zshrc"
    if [ ! -f "$zrc" ] || ! grep -qs "python@3.12/libexec/bin" "$zrc"; then
        {
            echo ""
            echo "# Added by whisper-studio setup.sh: prefer Homebrew Python 3.12"
            echo "export PATH=\"$py_bin:\$PATH\""
        } >> "$zrc"
        echo "Added Python 3.12 to PATH in $zrc."
    fi

    # Use it for the rest of THIS run (so the venv below is built with 3.12).
    export PATH="$py_bin:$PATH"
    hash -r 2>/dev/null || true
}

# ── Vector-search Python ──────────────────────────────────────────────────
# sqlite-vec needs an interpreter whose stdlib sqlite3 can load extensions. The
# python.org macOS framework build can't; Homebrew's can. We build the venv from
# an extension-capable interpreter, referenced by ABSOLUTE PATH only — the user's
# default python3 and shell config are left untouched. Falls back gracefully to
# the active python3 (the index then uses its numpy vector search).
VENV_PYTHON=""

_python_loads_extensions() {
    "$1" - >/dev/null 2>&1 <<'PYEOF'
import sqlite3, sys
try:
    sqlite3.connect(":memory:").enable_load_extension(True)
except Exception:
    sys.exit(1)
PYEOF
}

_select_venv_python() {
    if command -v python3 >/dev/null 2>&1 && _python_loads_extensions "$(command -v python3)"; then
        VENV_PYTHON="$(command -v python3)"
        return 0
    fi
    if ! command -v brew >/dev/null 2>&1; then
        echo "Note: this Python can't load SQLite extensions and Homebrew isn't present — vector search will use the numpy fallback (still works)."
        VENV_PYTHON="$(command -v python3)"
        return 0
    fi
    echo "Building the venv from a Homebrew Python so vector search can use sqlite-vec (your default python3 is untouched)..."
    local v cand
    for v in 3.13 3.12 3.11; do
        cand="$(brew --prefix)/opt/python@$v/bin/python3.$v"
        if [ -x "$cand" ]; then VENV_PYTHON="$cand"; break; fi
    done
    if [ -z "$VENV_PYTHON" ]; then
        echo "Installing python@3.13 via Homebrew (one-time, isolated to Homebrew)..."
        brew install python@3.13 >/dev/null 2>&1 || true
        VENV_PYTHON="$(brew --prefix)/opt/python@3.13/bin/python3.13"
    fi
    if [ ! -x "$VENV_PYTHON" ] || ! _python_loads_extensions "$VENV_PYTHON"; then
        echo "Note: couldn't get an extension-capable Python — vector search will use the numpy fallback."
        VENV_PYTHON="$(command -v python3)"
    fi
}

require_python_310() {
    local ver="" major=0 minor=0
    if command -v python3 >/dev/null 2>&1; then
        ver=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
        major=${ver%%.*}
        minor=${ver#*.}
    fi

    if [ -n "$ver" ] && _py_minor_ok "$major" "$minor"; then
        return 0
    fi

    if [ -n "$ver" ]; then
        echo "Python $ver detected; this project requires Python 3.10 or newer."
    else
        echo "Python 3 was not found; this project requires Python 3.10 or newer."
    fi

    _install_python_312

    # Re-check against the freshly installed Python.
    if ! command -v python3 >/dev/null 2>&1; then
        echo "ERROR: python3 still not found after install. Open a new terminal and re-run."
        exit 1
    fi
    ver=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
    major=${ver%%.*}
    minor=${ver#*.}
    if ! _py_minor_ok "$major" "$minor"; then
        echo "ERROR: Python is still $ver. Open a new terminal so the updated PATH applies, then re-run. Exiting."
        exit 1
    fi
    echo "Using Python $ver for this setup."
}
require_python_310

# Runs a noisy install command. Output goes to $SETUP_LOG on success
# (terminal stays clean — just the one-line progress message). On
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

# Prerequisite check for production runs. ffmpeg and ripgrep are runtime
# dependencies the app cannot start without, so on macOS we auto-install them
# via Homebrew when missing (same as dev mode) rather than failing the run. The
# remaining prerequisites (Homebrew itself, curl, lsof) are verified only and
# reported if missing.
#
# Collects all unmet items before exiting so the user can fix everything in a
# single pass instead of re-running the script after each one.
check_prod_prerequisites() {
    local missing=()
    local versions=()
    local is_mac=0
    [ "$(uname)" = "Darwin" ] && is_mac=1

    echo ""
    echo "Checking production prerequisites..."
    echo ""

    # Python 3.10+ (already validated by require_python_310; we just
    # capture the version string for the success report).
    versions+=("Python:    $(python3 --version 2>&1)")

    # Homebrew — macOS only. The other deps below have brew install
    # paths that won't work without it.
    if [ "$is_mac" -eq 1 ]; then
        if command -v brew >/dev/null 2>&1; then
            versions+=("Homebrew:  $(brew --version 2>&1 | head -n 1)")
        else
            missing+=("Homebrew not found. Install with:
      /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"")
        fi
    fi

    # Git and AWS CLI are intentionally not checked here — setup.sh
    # auto-installs them (see the venv block below) so requiring them
    # up front would double-gate. AWS CLI still needs ``aws configure``
    # from the user; that's a separate step we can't automate.

    # ffmpeg — mlx-whisper uses it to decode audio. Whisper itself won't start
    # without it on the path. Auto-install via Homebrew on macOS; on Linux or
    # without brew, report it as missing.
    if command -v ffmpeg >/dev/null 2>&1; then
        versions+=("ffmpeg:    $(ffmpeg -version 2>&1 | head -n 1)")
    elif [ "$is_mac" -eq 1 ] && command -v brew >/dev/null 2>&1; then
        run_quiet "Installing ffmpeg via Homebrew" brew install ffmpeg
        versions+=("ffmpeg:    $(ffmpeg -version 2>&1 | head -n 1)")
    elif [ "$is_mac" -eq 1 ]; then
        missing+=("ffmpeg not found. Install with: brew install ffmpeg")
    else
        missing+=("ffmpeg not found. Install with: apt-get install ffmpeg")
    fi

    # ripgrep — search backend used by the workspace tools. Auto-install via
    # Homebrew on macOS; on Linux or without brew, report it as missing.
    if command -v rg >/dev/null 2>&1; then
        versions+=("ripgrep:   $(rg --version 2>&1 | head -n 1)")
    elif [ "$is_mac" -eq 1 ] && command -v brew >/dev/null 2>&1; then
        run_quiet "Installing ripgrep via Homebrew" brew install ripgrep
        versions+=("ripgrep:   $(rg --version 2>&1 | head -n 1)")
    elif [ "$is_mac" -eq 1 ]; then
        missing+=("ripgrep not found. Install with: brew install ripgrep")
    else
        missing+=("ripgrep not found. Install with: apt-get install ripgrep")
    fi

    # curl — used by this script for backend/Vite health checks.
    if command -v curl >/dev/null 2>&1; then
        versions+=("curl:      $(curl --version 2>&1 | head -n 1)")
    else
        if [ "$is_mac" -eq 1 ]; then
            missing+=("curl not found. Install with: brew install curl  (pre-installed on macOS — check your PATH)")
        else
            missing+=("curl not found. Install with: apt-get install curl")
        fi
    fi

    # lsof — used by this script to find an available port.
    if command -v lsof >/dev/null 2>&1; then
        versions+=("lsof:      installed")
    else
        if [ "$is_mac" -eq 1 ]; then
            missing+=("lsof not found. Pre-installed on macOS — check your PATH.")
        else
            missing+=("lsof not found. Install with: apt-get install lsof")
        fi
    fi

    if [ ${#missing[@]} -gt 0 ]; then
        echo "ERROR: production-mode prerequisites are not met."
        echo ""
        echo "The following ${#missing[@]} item(s) need attention:"
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

    echo "All prerequisites met:"
    echo ""
    for line in "${versions[@]}"; do
        echo "  $line"
    done
    echo ""
}

# config.json handling (seed it on first run, back up and reset it on --new)
# happens after argument parsing below, once we know whether --new was passed.

# Truncate the install log so each run starts fresh.
: > "$SETUP_LOG"

FRESH=0
# Production is the default: a bare `bash setup.sh` builds the frontend and
# serves it from the backend, the same as passing `--prod`. Pass `--dev` for the
# Vite dev server with HMR.
PROD=1
ENV_FLAG=""   # tracks an explicit --dev/--prod so passing both is rejected
# Model mode: where indexing/RAG runs (cloud / hybrid / local). Only persisted
# when a flag is given, so a plain re-run honors the existing config; a fresh
# install gets the server-seeded first-run default (hybrid), same as the Mac app.
MODE_FLAG=""
# Auto-open the app in the default browser once everything is ready.
# Can also be disabled via NO_OPEN=1 env var (useful for SSH sessions
# where ``open`` would launch a phantom tab on the remote Mac's display).
NO_OPEN=${NO_OPEN:-0}
for arg in "$@"; do
    case "$arg" in
        --new|--fresh)
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
        --cloud)
            MODE_FLAG="cloud"
            ;;
        --hybrid)
            MODE_FLAG="hybrid"
            ;;
        --local)
            MODE_FLAG="local"
            ;;
        --no-open)
            NO_OPEN=1
            ;;
        -h|--help)
            echo "Usage: bash setup.sh [--new] [--dev|--prod] [--cloud|--hybrid|--local] [--no-open]"
            echo "  --new       Delete venv/, node_modules/, and static/dist/ and reinstall from scratch."
            echo "              Also backs up your config (timestamped) and resets it to the same"
            echo "              first-run defaults a fresh Mac-app install gets."
            echo "  --dev       Development mode: run the backend + Vite dev server with HMR."
            echo "  --prod      Production mode: build the frontend and serve it from the backend"
            echo "              only (no Vite dev server). This is the default."
            echo "  --cloud     Start in cloud mode: all indexing/RAG on Amazon Bedrock."
            echo "  --hybrid    Start in hybrid mode: pick a backend per capability."
            echo "  --local     Start in local mode: everything on-device."
            echo "  --no-open   Don't auto-open the app in the default browser."
            echo "              Also honored via NO_OPEN=1 env var."
            echo ""
            echo "No model weights are downloaded by this script. Exactly like the Mac app's"
            echo "first launch: transcription models download on your first recording, index"
            echo "models on your first index build, and chat models are installed on demand"
            echo "from Settings > Models > Discover."
            echo "Without a mode flag, an existing config is honored; a fresh install starts in"
            echo "hybrid mode (on-device index, cloud chat) — the Mac app's first-run default."
            echo ""
            echo "Default: production — builds the frontend and serves it from the backend,"
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

# Production runs do a strict pre-flight before touching the filesystem
# so the user gets a single, actionable list of missing tools instead of
# a half-installed venv and a cryptic Python traceback ten minutes later.
if [ "$PROD" -eq 1 ]; then
    check_prod_prerequisites
fi

if [ "$FRESH" -eq 1 ]; then
    # A fresh install resets the user config too: archive any existing layer as
    # a timestamped backup (it holds the user's Tavily key + per-machine
    # settings), then REMOVE it so the first-run seeding below recreates
    # config.user.json with the same defaults a fresh Mac-app install gets.
    # Both files are gitignored, so the backup is the only copy — never delete
    # without backing up first.
    for cfg_file in config.json config.user.json; do
        if [ -f "$cfg_file" ]; then
            config_backup="$cfg_file.bak.$(date +%Y%m%d%H%M%S)"
            cp "$cfg_file" "$config_backup"
            rm -f "$cfg_file"
            echo "Backed up $cfg_file to $config_backup and reset it."
        fi
    done
    echo "  Config will be re-seeded with first-run defaults (hybrid mode)."
    echo "  Re-add your Tavily API key and any custom settings in Settings (gear icon)."
    # pricing.json gets the same treatment (gitignored per-key rate overrides;
    # defaults live in pricing.example.json). Back up any existing one and reseed
    # so a fresh install starts from the current rate table.
    if [ -f pricing.json ]; then
        pricing_backup="pricing.json.bak.$(date +%Y%m%d%H%M%S)"
        cp pricing.json "$pricing_backup"
        echo "Backed up existing pricing.json to $pricing_backup."
    fi
    if [ -f pricing.example.json ]; then
        cp pricing.example.json pricing.json
        echo "Wrote a fresh pricing.json from pricing.example.json."
    fi
    if [ -d "$VENV_DIR" ]; then
        echo "Removing existing virtual environment ($VENV_DIR)..."
        rm -rf "$VENV_DIR"
    fi
    if [ -d "$NODE_MODULES_DIR" ]; then
        echo "Removing existing node_modules ($NODE_MODULES_DIR)..."
        rm -rf "$NODE_MODULES_DIR"
    fi
    # Also wipe the Vite build output. Without this, a --new run will
    # still serve a stale compiled bundle from static/dist/ even after
    # node_modules has been reinstalled — so frontend code changes
    # made since the last build won't take effect until the user
    # remembers to rerun `npm run build`. Forcing a clean slate is
    # what "fresh install" should actually mean.
    if [ -d "static/dist" ]; then
        echo "Removing existing frontend build (static/dist/)..."
        rm -rf static/dist
    fi
fi

# The user config is NOT copied from config.example.json any more: that file is
# the read-only SYSTEM layer (catalog + defaults), and a full copy of it is
# exactly what used to shadow shipped model/pricing updates after first run.
# Instead, once the venv exists, the first-run seeding below calls the same
# migrate_user_config() the packaged Mac app runs on boot, which creates a
# config.user.json holding only the first-run defaults (hybrid mode with the
# on-device index capabilities; weights download on demand).

# NOTE: new template models need no sync step. load_config merges
# DEFAULTS -> config.example.json -> the user layer additively, so a model
# added to config.example.json reaches every install on the next start.

# Seed pricing.json from the template on first run too. The app runs fine off
# pricing.example.json alone, but seeding gives a ready-to-edit local copy for
# per-key rate overrides. gitignored; an existing pricing.json is left untouched.
if [ ! -f pricing.json ] && [ -f pricing.example.json ]; then
    cp pricing.example.json pricing.json
    echo "Created pricing.json from pricing.example.json (edit to override model rates)."
fi

if [ -d "$VENV_DIR" ]; then
    echo "Virtual environment found, skipping Python setup."
    source "$VENV_DIR/bin/activate"
    if ! _python_loads_extensions "$VENV_DIR/bin/python"; then
        echo "  (Vector search is using the numpy fallback. For faster sqlite-vec search, delete venv/ and re-run this script with Homebrew available.)"
    fi
else
    _select_venv_python
    echo "Creating virtual environment (python: $VENV_PYTHON)..."
    "$VENV_PYTHON" -m venv "$VENV_DIR"
    source "$VENV_DIR/bin/activate"
    run_quiet "Installing Python dependencies" pip install -r requirements.txt

    # Live-preview browser: the Playwright pip package (requirements.txt) does
    # not bundle the browser binary, so fetch the Chromium build it expects.
    # Idempotent — Playwright skips the download if the build is already cached
    # under ms-playwright, so re-running setup.sh is cheap.
    run_quiet "Installing Playwright Chromium (live preview)" python -m playwright install chromium

    mkdir -p data

    # Install ffmpeg (needed by mlx-whisper for audio processing)
    if command -v ffmpeg >/dev/null 2>&1; then
        echo "ffmpeg already installed."
    elif command -v brew >/dev/null 2>&1; then
        run_quiet "Installing ffmpeg via Homebrew" brew install ffmpeg
    else
        echo "WARNING: ffmpeg not found. Please install ffmpeg for audio processing."
    fi

    # Install ripgrep (search engine dependency)
    if command -v rg >/dev/null 2>&1; then
        echo "ripgrep already installed."
    elif command -v brew >/dev/null 2>&1; then
        run_quiet "Installing ripgrep via Homebrew" brew install ripgrep
    else
        echo "ripgrep installed via pip package (requirements.txt)."
    fi

    # Install git. Needed at runtime for skill/plugin cloning and for
    # the workspace git integration. The user almost certainly has it
    # if they cloned this repo, but they may have grabbed a tarball.
    if command -v git >/dev/null 2>&1; then
        echo "git already installed."
    elif command -v brew >/dev/null 2>&1; then
        run_quiet "Installing git via Homebrew" brew install git
    else
        echo "WARNING: git not found and no Homebrew. Skill/plugin cloning will fail."
    fi

    # Install AWS CLI. Required for Bedrock-backed Claude inference;
    # the install does NOT configure credentials — the user still has
    # to run ``aws configure`` once with their access keys and the
    # Bedrock region.
    if command -v aws >/dev/null 2>&1; then
        echo "AWS CLI already installed."
    elif command -v brew >/dev/null 2>&1; then
        run_quiet "Installing AWS CLI via Homebrew" brew install awscli
        echo "  Run 'aws configure' to set your access keys + Bedrock region."
    else
        echo "WARNING: AWS CLI not found and no Homebrew. Chat (Bedrock) will fail until installed."
    fi

    # Install isolated Node.js into the venv (for ESLint LSP)
    run_quiet "Installing Node.js $NODE_VERSION into virtual environment" \
        nodeenv --python-virtualenv --node="$NODE_VERSION"
    source "$VENV_DIR/bin/activate"
fi

if [ -d "$NODE_MODULES_DIR" ]; then
    echo "node_modules found, skipping npm install."
else
    run_quiet "Installing npm dependencies" npm install
fi

# Build frontend in production mode
if [ "$PROD" -eq 1 ]; then
    run_quiet "Building frontend" npm run build
fi

# ── First-run config seeding (Mac-app parity) ────────────────────────────────
# A fresh checkout has no user config layer. The packaged app's boot creates one
# via migrate_user_config() (bootstrap_home only runs when WHISPER_HOME is set),
# seeding the first-run defaults: hybrid mode with the on-device index
# capabilities, every weight downloading on demand. Run the exact same code path
# here so a setup.sh install starts identically to the Mac app's first launch.
# An existing config (either layer) is left alone — the user's settings win.
if [ ! -f config.json ] && [ ! -f config.user.json ]; then
    if python -c "from server.infrastructure.config import migrate_user_config; migrate_user_config()" >>"$SETUP_LOG" 2>&1 && [ -f config.user.json ]; then
        echo "Seeded config.user.json with first-run defaults (hybrid mode, on-device index) — the Mac app's first-launch config."
    else
        echo "WARNING: could not seed the first-run config; the backend will run on shipped defaults (see $SETUP_LOG)."
    fi
fi

# ── Persist an explicit model-mode flag ──────────────────────────────────────
# Only when a flag was passed: write it into the ACTIVE user layer
# (config.user.json once it exists — including the one just seeded — else the
# legacy config.json; mirrors config._active_user_config_path). Without a flag
# the existing config, or the seeded first-run default, decides the mode.
if [ -n "$MODE_FLAG" ]; then
    python - "$MODE_FLAG" <<'PYEOF' || true
import json, os, sys
mode = sys.argv[1]
if os.path.exists("config.user.json") or not os.path.exists("config.json"):
    target = "config.user.json"
else:
    target = "config.json"
cfg = {}
if os.path.exists(target):
    try:
        with open(target) as f: cfg = json.load(f)
    except Exception: cfg = {}
cfg["model_mode"] = mode
cfg["local_mode"] = (mode != "cloud")
with open(target, "w") as f: json.dump(cfg, f, indent=2)
print(f"Set model_mode={mode} in {target}")
PYEOF
fi

# ── On-device serving runtime: llama.cpp (llama-server) ─────────────────────
# The ONLY on-device chat backend. llama-server owns upstream's per-model-family
# tool-call and reasoning parsers, so a GGUF added to config.json gets tools and
# a thinking channel with no code change.
#
# Installed in EVERY mode, matching the Mac app (which bundles the binary): a
# model installed later from Settings > Models > Discover must serve without the
# user re-running setup. It is a small runtime — no model weights come with it.
#
# Build matters: gemma4 (and newer architectures) need >= LLAMA_MIN_BUILD. Older
# builds load fine and then fail with "unknown model architecture", so we upgrade
# an outdated install rather than leaving that trap. Failure here is REPORTED, not
# silently absorbed: without llama-server, on-device chat does not work at all
# (there is no in-process fallback), so a broken install must be visible.
LLAMA_MIN_BUILD=10090

llama_server_build() {
    command -v llama-server >/dev/null 2>&1 || return 1
    # Prints e.g. "version: 10090 (7347430f4)" on stderr.
    llama-server --version 2>&1 | sed -n 's/^[[:space:]]*version:[[:space:]]*\([0-9]\{1,\}\).*/\1/p' | head -1
}

LLAMA_BUILD="$(llama_server_build || true)"
if [ -z "$LLAMA_BUILD" ]; then
    if command -v brew >/dev/null 2>&1; then
        echo "Installing llama.cpp via Homebrew — provides the llama-server binary (logs → $SETUP_LOG)..."
        if brew install llama.cpp >>"$SETUP_LOG" 2>&1; then
            LLAMA_BUILD="$(llama_server_build || true)"
            echo "  ✓ llama-server installed (build ${LLAMA_BUILD:-unknown}) at $(command -v llama-server)."
        else
            echo "  ✗ llama.cpp install FAILED (see $SETUP_LOG)."
            echo "    On-device chat will not work until this succeeds — there is no"
            echo "    fallback runtime. Retry with: brew install llama.cpp"
        fi
    else
        echo "  ✗ Homebrew not found, so llama.cpp cannot be installed automatically."
        echo "    On-device chat REQUIRES llama-server and has no fallback. Install it"
        echo "    manually: https://github.com/ggml-org/llama.cpp"
    fi
elif [ "$LLAMA_BUILD" -lt "$LLAMA_MIN_BUILD" ] 2>/dev/null; then
    echo "llama.cpp build $LLAMA_BUILD is older than $LLAMA_MIN_BUILD (needed for current"
    echo "model architectures). Upgrading (logs → $SETUP_LOG)..."
    if command -v brew >/dev/null 2>&1 && brew upgrade llama.cpp >>"$SETUP_LOG" 2>&1; then
        echo "  ✓ llama.cpp upgraded to build $(llama_server_build || echo unknown)."
    else
        echo "  ✗ Could not upgrade llama.cpp. On-device models may fail with 'unknown"
        echo "    model architecture'; run 'brew upgrade llama.cpp' manually."
    fi
else
    echo "  ✓ llama-server present (build $LLAMA_BUILD) at $(command -v llama-server)."
fi

# ── Model weights: nothing is downloaded here ────────────────────────────────
# Exactly like the Mac app's first launch, every model downloads ON DEMAND with
# an in-app progress banner, into ./models (idempotent, resumable):
#   * transcription engines + the speaker encoder — on your first recording (or
#     pre-pull them from Settings > Models > Transcription),
#   * on-device index weights (Qwen3 embed/rerank, GLiNER) — on your first index
#     build in hybrid/local mode,
#   * on-device chat models — installed from Settings > Models > Discover
#     (the app ships with none).
echo ""
echo "No model weights are downloaded by setup. Models download on demand:"
echo "  transcription on your first recording, index models on your first index"
echo "  build, and chat models from Settings > Models > Discover."

echo ""
echo "Setup complete. Starting server..."

# Find an available port starting from 8000
PORT=8000
while lsof -i :"$PORT" >/dev/null 2>&1; do
    PORT=$((PORT + 1))
done
export PORT

# Only the packaged macOS shell owns the backend parent-watchdog contract.
# A dev server launched from an app terminal can inherit its outer shell's
# WHISPER_PARENT_PID; passing that stale PID to this new backend makes it exit
# as soon as startup completes. setup.sh is the direct parent here, so clear it.
unset WHISPER_PARENT_PID

python -m server.main &
SERVER_PID=$!

# Wait for the backend to be ready before starting Vite. Startup no longer
# loads any model (weights download and load on demand), but a cold first boot
# still compiles bytecode and opens the databases, so keep a generous ceiling.
echo "Waiting for backend (up to 90s on a cold first boot)..."
for i in $(seq 1 90); do
    if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
        echo "Backend ready after ${i}s."
        break
    fi
    sleep 1
done
if ! curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "WARNING: backend did not respond on /health within 90s. Check logs." >&2
fi

VITE_PID=""
if [ "$PROD" -eq 0 ]; then
    # Find an available port for Vite starting from 5173
    VITE_PORT=5173
    while lsof -i :"$VITE_PORT" >/dev/null 2>&1; do
        VITE_PORT=$((VITE_PORT + 1))
    done

    # Forward the chosen backend port to vite so its proxy targets the
    # right server when the backend lands on 8001/8002/... A separate
    # var (not ``PORT``) because vite reads ``PORT`` as its own listen
    # port, which would clash with ``--port``.
    BACKEND_PORT="$PORT" npx vite --port "$VITE_PORT" &
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

# In dev mode, Vite has just been spawned in the background — wait for
# it to actually start serving before we tell the browser to open it.
# Otherwise the browser races Vite and shows "site can't be reached".
# 15s ceiling is generous; Vite typically responds within ~2s.
if [ "$PROD" -eq 0 ]; then
    echo "Waiting for Vite dev server..."
    for i in $(seq 1 30); do
        if curl -sf "$APP_URL" >/dev/null 2>&1; then
            break
        fi
        sleep 0.5
    done
    if ! curl -sf "$APP_URL" >/dev/null 2>&1; then
        echo "WARNING: Vite did not respond on $APP_URL within 15s. Check logs." >&2
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

# Auto-open the right URL in the default browser. Only opens on systems
# where ``open`` exists (macOS, plus some Linux setups that alias it).
# Silently skipped on systems without it — the URL is already printed.
if [ "$NO_OPEN" -eq 0 ]; then
    if command -v open >/dev/null 2>&1; then
        echo "Opening $APP_URL in your default browser..."
        open "$APP_URL" 2>/dev/null || true
    fi
fi

echo "Press Ctrl+C to stop."

trap "kill $SERVER_PID 2>/dev/null; [ -n \"$VITE_PID\" ] && kill $VITE_PID 2>/dev/null" EXIT
wait $SERVER_PID
