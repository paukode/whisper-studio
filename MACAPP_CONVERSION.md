# Whisper Studio as a native macOS app (Apple Silicon)

Investigation of what it takes to ship Whisper Studio as a signed, notarized
`.app` on a DMG: user drags it to /Applications, opens it, downloads the models
they want (Whisper, Parakeet, the indexing models, and the local GGUF chat
models) from inside the app, and their existing `~/.aws` credentials keep
working for Bedrock.

This folder (`whisper-studio-macapp`) is a clean git clone of the repo taken on
2026-08-03 as the basis for this work. The original checkout is untouched.
Gitignored runtime artifacts (`config.json`, `pricing.json`, `models/`, `venv/`,
`storage/`, `data/`, `node_modules/`) were deliberately not copied; run
`bash setup.sh` here when work starts, or symlink `models/` from the original
checkout to avoid re-downloading ~38 GB of weights.

## Verdict

Feasible, and a better fit than most web apps get, because the hard
architectural properties are already in place:

- Single local process, single origin: FastAPI serves the API and the built
  React SPA on `127.0.0.1:$PORT` (`server/main.py:324-389`). No separate
  frontend host to bundle.
- The ASR stack is already Apple Silicon native: Parakeet and Whisper both run
  on MLX/Metal (`parakeet-mlx`, `mlx-whisper`), not CUDA or CPU-only tort paths.
- Models are already downloaded lazily at runtime via `huggingface_hub`
  (`snapshot_download` / `hf_hub_download`) with presence checks, so "user
  downloads models after install" is an extension of existing code, not new
  machinery.
- AWS is already on the default botocore credential chain. Every client is
  `boto3.client(..., region_name=...)` with region from `config.json`
  (`server/chat/infra.py:40-52` and 8 sibling call sites). No profile names, no
  stored keys. `~/.aws` just works, provided we do NOT adopt the App Sandbox.

The work is packaging engineering plus one focused refactor (path relocation),
not an architecture change. Realistic scope: 4 to 6 weeks of focused work,
phased below so a working prototype lands in week 2 or 3.

## What the app assumes today (the gap list)

### 1. Writable directories inside the repo tree

A signed `.app` bundle is read-only (and Gatekeeper "app translocation" runs a
freshly downloaded app from a randomized read-only path on first launch). These
currently write next to the code:

| Path | Written by | Override today |
|---|---|---|
| `config.json` | Settings PUT, feature flags (`server/infrastructure/config.py:435-444`) | none |
| `pricing.json` | seeded by setup.sh, read by `server/costs/tracker.py:46-47` | none |
| `models/` + `models/.logs/` | all HF downloads (ASR, embedder, reranker, GLiNER, GGUFs) and llama-server logs (`server/local/runtime.py:27-28`, `server/local/llama_server.py:254`, `server/asr/*_backend.py`, `server/diarization/speakers.py:38-41`, `server/index/config.py:13-22`) | none |
| `storage/` | sessions.db, per-workspace index DBs, attachments (`server/infrastructure/sessions.py:15-17`, `server/index/paths.py:19-20`, `server/attachment_store.py:32-33`) | none |
| `data/` | memory, cron, hooks, result cache via `data_root()` (`server/infrastructure/paths.py:25-41`) | `WHISPER_DATA_DIR` env or `data_dir` config (only this one) |
| `skills/` | skill imports git-clone into it (`server/skills_import.py:236`) | none |
| `plugins/` | `init_plugins` mkdir + README write (`server/plugins.py:154-155`) | none |
| `venv/` | preview feature pip-installs playwright at runtime (`server/preview/install_routes.py:44-60`) | n/a |

Every one of these resolves the repo root from `__file__` (three `dirname()`
calls up), plus `os.chdir(BASE_DIR)` at startup (`server/main.py:381`).

Fix: introduce a single `app_home()` in `server/infrastructure/paths.py`,
defaulting to the repo root when running from a checkout (today's behavior,
zero migration for dev) and to `~/Library/Application Support/WhisperStudio/`
when `WHISPER_HOME` is set (which the app shell sets). Route all eight path
roots through it. This is plain Python work, fully testable in this clone with
the existing pytest suite before any packaging exists.

### 2. External binaries expected from Homebrew / PATH

Finder-launched apps get `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, no
`/opt/homebrew/bin`, and none of the user's shell env. Current resolution:

| Binary | Resolution today | Plan for the bundle |
|---|---|---|
| `llama-server` (min build 10090) | `shutil.which` then `/opt/homebrew/bin`, `/usr/local/bin` (`server/local/llama_server.py:40-69`) | Bundle a pinned llama.cpp release in `Contents/Resources/bin/`. Use the official `llama-<build>-bin-macos-arm64.zip` release artifacts (relocatable, dylibs alongside the binary, Metal shaders embedded) rather than the brew binary, which links dylibs under `/opt/homebrew` and is not redistributable as-is. Add a `WHISPER_LLAMA_SERVER_PATH` env/config override checked before PATH. |
| `ffmpeg` / `ffprobe` | bare name, PATH only (`server/extract/media.py:275-286`, also required by `mlx_whisper.audio.load_audio`) | Bundle a static arm64 ffmpeg (~30-70 MB, or a minimal build with just the demuxers/decoders needed). Add `WHISPER_FFMPEG_PATH`. Only the file/attachment transcription path needs it; live mic transcription is pure WebSocket + numpy and needs no ffmpeg. |
| `rg` (ripgrep) | `WHISPER_RG_PATH` env, PATH, then the pip `ripgrep` wheel fallback (`server/search/engine.py:39-73`) | Already solved: the pip wheel ships a binary. Fix the one bare `["rg", ...]` spawn in `server/workspace/routes/search.py:86` to use the same resolver. |
| `git` | `shutil.which("git") or "git"` (`server/git/core.py:67-69`) | Do not bundle. `/usr/bin/git` exists once Xcode CLT is installed; on machines without it, degrade the Git panel gracefully with a "install Command Line Tools" hint. Normal for dev-audience apps. |
| `node` | `shutil.which("node") or "/usr/local/bin/node"` (`server/workflows/runtime.py:35`); nodeenv-in-venv provides it today for eslint/tsls | Bundle a node binary (~45 MB) in Resources/bin for the workflow harness, eslint, and typescript-language-server; point the code at it via env. Note the workflow harness scrubs PATH to `/usr/local/bin:/usr/bin:/bin:/sbin` (`runtime.py:54`), which must learn the bundled path. |
| `aws` CLI | PATH (`server/executors/code.py:164-186`) | Do not bundle (huge). Chat/Bedrock never needs it (boto3 is a library). The `aws_cli` executor tool degrades with a clear message if absent. |
| `pylsp` | from the venv (`server/lsp_proxy.py:27-47`) | Comes free with the bundled Python site-packages. |
| `typescript-language-server` | PATH | Ship the npm package tree in Resources and launch with bundled node, or degrade. |
| `sandbox-exec`, `launchctl`, `open`, `$SHELL` | OS-provided | Nothing to do. |
| `gh` | `shutil.which`, optional | Already degrades gracefully. |
| Playwright Chromium | separate download into `~/Library/Caches/ms-playwright` | Already outside the repo. Vendor the `playwright` pip package in the bundle and keep only the browser download at runtime; disable the pip-install endpoint (`server/preview/install_routes.py`), it cannot write into a signed bundle. |

### 3. Python runtime

setup.sh insists on a Homebrew CPython specifically because sqlite-vec needs
`sqlite3.enable_load_extension` and the python.org framework build disables it
(`setup.sh:61-104`, probe quoted there; loader at
`server/index/store/base.py:269-285` with a numpy brute-force fallback).

For the bundle: use python-build-standalone (arm64, CPython 3.13). It enables
sqlite extension loading, is relocatable, and is the de facto standard for
embedding Python in apps. Ship it in `Contents/Resources/python/` with a fully
populated `site-packages` (the current venv is 2.2 GB; expect roughly that,
torch is the dominant chunk).

Deliberately do NOT freeze with PyInstaller/py2app: torch + MLX Metal kernels +
transformers + speechbrain + numba/llvmlite + pyobjc is exactly the dependency
set that fights freezers. A plain runtime-folder layout sidesteps all of it;
notarization only requires that every `.so`/`.dylib`/executable in the bundle
is codesigned, which is a `find | codesign` loop in the build script.

Python 3.13 note: `venv/bin/python` today is Homebrew `python@3.13.14` and all
wheels in requirements.txt already resolve for it on arm64, so no version work.

### 4. The launchd index-refresh agent

`server/index/agent.py:26-121` writes
`~/Library/LaunchAgents/com.whisperstudio.indexrefresh.plist` whose
`ProgramArguments` bakes in `<repo>/venv/bin/python` and
`WorkingDirectory=<repo>`. In the app this must point at the bundled runtime
inside `/Applications/Whisper Studio.app/...`. Two robust options:

- Re-register the plist on every app launch with the current bundle path
  (cheap, handles app moves/updates), or
- Adopt `SMAppService` with a proper helper (nicer, more work).

Either way this is a small, contained change.

## Target architecture

```
Whisper Studio.app/
  Contents/
    MacOS/WhisperStudio          <- thin native shell (Swift + WKWebView)
    Info.plist                   <- NSMicrophoneUsageDescription, LSMinimumSystemVersion, arm64-only
    Resources/
      python/                    <- python-build-standalone + site-packages (~2.5 GB)
      server/                    <- the backend package + skills defaults + config.example.json
      static/dist/               <- vite build output
      bin/llama-server, *.dylib  <- pinned llama.cpp release (build >= 10090)
      bin/ffmpeg, bin/node, ...
```

Shell responsibilities (a few hundred lines of Swift):

1. Pick a free port, set env (`WHISPER_HOME`, `PORT`, `WHISPER_LLAMA_SERVER_PATH`,
   `WHISPER_FFMPEG_PATH`, ...), spawn `Resources/python/bin/python -m server.main`
   as a child process, poll `/health` (the endpoint exists), then load
   `http://127.0.0.1:$PORT` in a WKWebView window.
2. Single-instance guard; on second launch, focus the window.
3. Clean shutdown on quit (the backend lifespan already stops llama-server and
   reaps orphans, `server/main.py:200-252`).
4. Menu bar: Quit, Open in Browser (escape hatch for anything WKWebView is fussy
   about, e.g. Chrome-tab audio capture for meetings), Show Logs, Check for Updates.
5. WKUIDelegate `requestMediaCapturePermission` handler so `getUserMedia` works
   in-app (mic permission surfaces as a normal macOS prompt against the app's
   `NSMicrophoneUsageDescription`).

Why Swift + WKWebView rather than Electron or Tauri: the UI is already a
complete SPA served by the backend, so the shell is genuinely thin; WKWebView
adds ~0 MB against Electron's ~200 MB; Sparkle integrates natively for updates;
and there is no cross-platform requirement (the ASR stack is MLX, macOS-only by
construction). Tauri v2 is the fallback choice if Rust is preferred over Swift.

## Model download manager (the in-app "download the models" requirement)

What exists: every model already lazy-downloads on first use with presence
checks, from public HF repos, into `models/` (no token needed):

| Model | Repo | Size | Trigger today |
|---|---|---|---|
| Whisper large-v3-turbo | `mlx-community/whisper-large-v3-turbo` | 1.5 GB | first batch transcription |
| Parakeet TDT 0.6B v3 | `mlx-community/parakeet-tdt-0.6b-v3` | 2.3 GB | first record (also warmed at startup) |
| ECAPA speaker encoder | `speechbrain/spkrec-ecapa-voxceleb` | 85 MB | first diarized utterance |
| Qwen3 embedding 0.6B | `Qwen/Qwen3-Embedding-0.6B` | 1.1 GB | first index build |
| Qwen3 reranker 0.6B | (per `server/index/config.py`) | 1.1 GB | first reranked query (opt-in) |
| GLiNER large v2.5 | `gliner-community/gliner_large-v2.5` | 1.7 GB | first NER extraction |
| GLiNER2 large | `fastino/gliner2-large-v1` | 1.8 GB | per-workspace opt-in |
| Local chat GGUFs | config-driven registry (`server/local/registry.py`) | 5-8 GB each | first local chat turn |

What to build: a first-run / Settings "Models" panel that lists these with
sizes, lets the user tick what they want (presets: "Transcription only" ~3.9 GB,
"+ Indexing" ~9 GB, "+ Local chat" per-model), streams download progress over
SSE, checks free disk space, and supports cancel/resume (huggingface_hub
already resumes partial downloads). The backend endpoints wrap the existing
`ensure_*` functions; the GGUF list already comes from the config registry, so
new models remain a config-only addition. Moderate frontend + thin API work.

One trap found: the GLiNER snapshot lacks its DeBERTa backbone tokenizer, and
the code pre-caches it into `~/.cache/huggingface` then loads with
`HF_HUB_OFFLINE=True` (`server/index/extractor.py:182-260`). The download
manager must run that pre-cache step as part of "download GLiNER", or first
extraction needs network.

## AWS credentials from ~/.aws

Confirmed clean: all nine boto3 call sites use the default chain with only
`region_name` from `config.json` (`bedrock_region`, default `us-east-1`).
The OpenAI-on-Bedrock path mints its bearer token the same way
(`aws_bedrock_token_generator`, `server/openai_bedrock/runtime.py:113-126`).
Consequences for the app:

1. Do not adopt the App Sandbox. Reading `~/.aws`, spawning llama-server, the
   PTY terminal, `sandbox-exec`, and user-configured MCP servers are all
   incompatible with it. Distribution is Developer ID + notarization (exactly
   the DMG flow requested). The Mac App Store is off the table, by design.
2. No TCC prompt guards `~/.aws`; a non-sandboxed signed app reads it freely.
3. New gap to cover: launched from Finder, the app inherits no shell env, so
   users who select credentials via `AWS_PROFILE` (or use `aws sso login`
   sessions tied to a profile) lose that. Add an optional "AWS profile" field in
   Settings that the backend applies (e.g. sets `AWS_PROFILE` before client
   creation). Users on the `[default]` profile need nothing. For expired SSO
   sessions, surface doctor's existing credential check
   (`server/doctor.py:24-46`) with a "run `aws sso login` in Terminal" hint.
4. The internal tool sandbox already re-allows `~/.aws` for the aws executors
   (`server/executors/code.py:19`, `server/sandbox.py:38-46`); keep that intact.

## Signing, notarization, DMG

- Requires an Apple Developer Program membership ($99/yr) and a
  Developer ID Application certificate.
- Build script signs inside-out: every Mach-O in `Resources/python` and
  `Resources/bin` (thousands of `.so`/`.dylib` files, a scripted `find` +
  `codesign --force --options runtime --timestamp`), then the shell, then the app.
- Hardened runtime entitlements needed:
  - `com.apple.security.cs.allow-jit` and/or
    `com.apple.security.cs.allow-unsigned-executable-memory`: numba/llvmlite
    (pulled by umap-learn and librosa) JIT-compiles at runtime; MLX compiles
    Metal kernels (fine) but its lazy graph engine is safe. Test which of the
    two suffices; start with both, tighten later.
  - `com.apple.security.cs.disable-library-validation`: lets the bundled Python
    load extension modules without fighting team-ID checks; often avoidable when
    everything is signed with the same identity, but keep it during bring-up.
  - Microphone: `NSMicrophoneUsageDescription` in Info.plist (TCC prompt fires
    on first record via WKWebView).
- Notarize with `xcrun notarytool submit --wait`, `xcrun stapler staple`, then
  build the DMG (`create-dmg` or `dmgbuild`) with the /Applications symlink and
  a background image. Sign the DMG too.
- App translocation: because a quarantined app can run from a randomized
  read-only mount on first launch, nothing may compute paths relative to the
  bundle for writing (covered by the `WHISPER_HOME` work) and the launchd plist
  must not be written until the app runs from its real location (or is simply
  re-written each launch).
- Estimated DMG size with the full site-packages and binaries: roughly 1.2 to
  1.5 GB compressed (torch ~1 GB uncompressed is the main cost; MLX, scipy,
  transformers, node, ffmpeg, llama-server make up the rest). If that stings, a
  later optimization is moving the torch-dependent indexing stack into a
  downloadable "runtime pack", but do not do this for v1: runtime-installed
  native code complicates signing and Gatekeeper. Ship it all, signed.

## Feature-by-feature behavior in the packaged app

| Feature | Status in .app |
|---|---|
| Live transcription (Parakeet/Whisper) + diarization | Works; pure MLX + WebSocket + numpy + webrtcvad wheel. Mic needs the TCC prompt via the shell. |
| Chat via Bedrock / OpenAI-on-Bedrock | Works off `~/.aws` unchanged; add the AWS-profile setting for non-default-profile users. |
| Local chat (llama-server) | Works with the bundled llama-server; no Homebrew needed anymore. |
| Indexing / GraphRAG / rerank | Works; torch+transformers bundled; sqlite-vec via pip wheel on extension-capable Python. |
| Attachments (OCR, PDF, media) | Works; ocrmac/pyobjc + pypdfium2 + markitdown bundle fine; ffmpeg bundled for media. |
| Workspace IDE: files, editor, terminal, search | Works; PTY uses `$SHELL`/`/bin/zsh`, rg from pip wheel. |
| Git panel, gh, CI | Needs Xcode CLT git on the machine; degrade with a hint if missing. |
| LSP (pylsp / tsls), eslint | pylsp free from bundled Python; tsls/eslint need the bundled node + npm trees, or degrade in v1. |
| Preview (Playwright) | Vendor pip package, keep browser download at runtime, remove the runtime pip-install endpoint. |
| Cron, memory, skills, plugins, MCP | Work once paths route through `WHISPER_HOME`; MCP servers the user configures still resolve from their own commands. |
| Meeting capture from a Chrome tab | Browser-dependent flow; keep the "Open in Browser" menu item as the supported path. |

## Phased plan

Phase 0, path + binary indirection (in this clone, no packaging yet), ~1 to 1.5 weeks
- `WHISPER_HOME` app-home layer routing config/pricing/models/storage/data/skills/plugins.
- Binary resolver with env/config overrides for llama-server, ffmpeg, node; fix the bare `rg` spawn.
- launchd plist re-registration on start.
- Models API: list/status/download-with-progress endpoints wrapping existing `ensure_*`.
- All existing tests stay green; app still runs identically from a checkout.

Phase 1, unsigned prototype .app, ~1 to 2 weeks
- Build script: vite build, fetch python-build-standalone, `pip install` into it,
  copy backend + static + binaries, assemble the bundle, ad-hoc sign.
- Swift WKWebView shell (launch, health poll, window, menu, mic delegate, quit).
- First-run onboarding: AWS credential check (reuse doctor), model download screen.
- Manual test pass across the feature table above on a machine without Homebrew.

Phase 2, distribution, ~1 week
- Developer ID signing of the full tree, notarization, stapling, DMG.
- Entitlement tightening (JIT test with numba, library validation).
- Translocation and clean-machine (no CLT, no brew) testing.

Phase 3, lifecycle, ~1 week
- Sparkle 2 auto-updates with a signed appcast (models and user data live in
  Application Support, so updates never touch them).
- Migration/import for existing repo-checkout users (point `WHISPER_HOME` at, or
  copy from, the old repo dirs).
- CI release pipeline (GitHub Actions macos arm64 runner, signing secrets),
  noting that release builds must run somewhere with working Actions billing or
  locally via a `make dmg` target.

## Open decisions

1. Shell technology: Swift + WKWebView (recommended) vs Tauri v2 vs a minimal
   menu-bar app that opens the default browser (cheapest, loses the "feels like
   an app" factor and the mic-permission story is the browser's).
2. v1 scope for dev-tool features on clean Macs: degrade (recommended) vs
   requiring Xcode CLT.
3. Whether tsls/eslint ship in v1 or degrade until a later release.
4. Bundle identity and name (e.g. `com.paukode.whisper-studio`), icon, minimum
   macOS version (MLX effectively requires macOS 13.5+; recommend 14+).
5. Apple Developer account: personal vs organization.
