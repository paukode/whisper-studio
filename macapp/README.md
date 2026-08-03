# Whisper Studio — macOS app packaging

Builds a self-contained `Whisper Studio.app` (Apple Silicon, macOS 14+):
a thin Swift/WebKit shell that spawns the bundled FastAPI backend
(standalone CPython 3.13) and shows the SPA in a WKWebView.

## Build

```bash
npm ci                      # once, at the repo root
bash macapp/build_app.sh    # produces dist-app/Whisper Studio.app
bash macapp/make_dmg.sh     # produces dist-app/WhisperStudio-<version>.dmg
```

Downloads (Python runtime, llama-server, ffmpeg/ffprobe, node) are cached in
`build-app/downloads/` and stages are skipped when already done; re-runs are
fast. Versions and checksums are pinned at the top of `build_app.sh`.

The compiled shell lives at `build-app/shell/WhisperStudio`; rebuild it alone
with `bash macapp/shell/build_shell.sh`.

## Env contract (shell -> backend)

The shell finds a free port, then spawns `python3 -m server.main` with
cwd = `Contents/Resources/backend` and this environment:

| Variable | Value |
| --- | --- |
| `HOST` | `127.0.0.1` |
| `PORT` | free port picked at launch |
| `WHISPER_HOME` | `~/Library/Application Support/WhisperStudio` (a pre-set `WHISPER_HOME` is respected, for testing) |
| `WHISPER_BIN_DIR` | `Contents/Resources/bin` (backend prepends it to PATH) |
| `WHISPER_LLAMA_SERVER_PATH` | `Resources/bin/llama-server` |
| `WHISPER_FFMPEG_PATH` | `Resources/bin/ffmpeg` |
| `WHISPER_FFPROBE_PATH` | `Resources/bin/ffprobe` |
| `WHISPER_NODE_PATH` | `Resources/bin/node` |
| `PATH` | `Resources/bin` prepended to the inherited PATH |

Backend stdout/stderr are appended to `$WHISPER_HOME/logs/backend.log` with a
timestamp line per launch ("Show Logs" in the Whisper Studio menu reveals it).
The shell polls `/health` for up to 120 s before showing the UI, and on quit
sends SIGTERM, waits up to 15 s, then SIGKILLs the backend.

## Signing and notarization

Default is ad-hoc signing (`SIGN_IDENTITY=-`): runs locally, but other Macs
will refuse to open it without right-click > Open. Hardened runtime is
deliberately skipped for ad-hoc builds (the combination breaks launch).

With a Developer ID certificate:

```bash
SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" bash macapp/build_app.sh
bash macapp/make_dmg.sh

# Notarize + staple (requires an App Store Connect API key or app-specific
# password configured as a notarytool keychain profile):
xcrun notarytool submit dist-app/WhisperStudio-<version>.dmg \
    --keychain-profile "notary" --wait
xcrun stapler staple "dist-app/Whisper Studio.app"
xcrun stapler staple dist-app/WhisperStudio-<version>.dmg
```

Real-identity builds sign with `--options runtime --timestamp` and
`macapp/entitlements.plist` (allow-jit, allow-unsigned-executable-memory,
disable-library-validation, audio-input) — required by CPython/torch and
llama.cpp Metal under the hardened runtime.

Bundle id defaults to `io.paukode.whisper-studio` (override with `BUNDLE_ID`).
Version comes from `git describe --tags --always`, falling back to `0.1.0`.

## Known v1 limitations

- Only `bin/node` is bundled (no npm), so `nodeenv`-provisioned helpers like
  the TypeScript language server and eslint degrade gracefully or are
  unavailable; Python linting (pyflakes/pylsp) still works.
- `gh` and `git` tooling is not bundled; those features need Command Line
  Tools / Homebrew installs on the host.
- Chrome-tab capture stays in the browser: use "Open in Browser" from the
  Whisper Studio menu for flows that need a real Chrome tab.
- The app icon is a plain solid-colour placeholder.
- Intel Macs are unsupported (arm64-only binaries throughout).
