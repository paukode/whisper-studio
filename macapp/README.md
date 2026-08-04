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

## Audio sources (native system/app capture)

The shell can capture the Mac's audio OUTPUT and feed it to transcription,
alongside or instead of the microphone. In the app, open the headphones menu
next to Record:

- **Microphone** — always available; can be turned off only while a native
  source is armed ("native only" mode, which never calls getUserMedia).
- **System audio** — everything the Mac plays (our own process is excluded).
- **One app** (e.g. Zoom) — the per-app list shows processes currently
  producing audio and refreshes each time the menu opens.

The selection persists across launches. All sources are mixed into ONE
16 kHz mono stream in the frontend, so the backend websocket contract is
unchanged and speaker diarization separates the voices. Wear headphones so
captured playback is not picked up a second time by the mic.

Implementation: Core Audio process taps (`CATapDescription` +
`AudioHardwareCreateProcessTap` + a private aggregate device + IOProc) in
`macapp/shell/NativeAudioCapture.swift`, converted to 16 kHz mono Int16 with
`AVAudioConverter` and pushed to the SPA over a WKWebView bridge
(`window.webkit.messageHandlers.nativeAudio` /
`window.__whisperNativeAudio`). The frontend wrapper is
`src/services/nativeAudioSource.ts`.

**Requirements and permission**

- macOS **14.4 or later** (the taps API). On older systems the shell injects
  `available:false` and the menu shows no native section.
- TCC: the first capture start triggers the OS **System Audio Recording**
  prompt (`NSAudioCaptureUsageDescription` in `Info.plist`). If denied, the
  UI shows an actionable error; re-enable under
  System Settings > Privacy & Security > Screen & System Audio Recording.

**Enumeration smoke test (no TCC needed)**

```bash
swiftc -D SMOKE_CLI -target arm64-apple-macos14 \
    macapp/shell/NativeAudioCapture.swift macapp/shell/smoke_list_sources.swift \
    -o /tmp/smoke_list_sources \
    -framework AppKit -framework WebKit -framework CoreAudio \
    -framework AudioToolbox -framework AVFoundation
/tmp/smoke_list_sources   # JSON: System audio + apps currently playing
```

**Manual test checklist**

1. Build and launch the app, play music (Music/Safari/`afplay`), open the
   headphones menu → the playing app appears in "This Mac".
2. Pick **System audio**, press Record — first time, expect the OS
   permission prompt; accept. Speak AND keep the music playing: the
   transcript should contain the music's lyrics/speech and your voice, with
   diarization splitting speakers. The header shows "Mic + System audio".
3. Stop. Pick the app source instead (e.g. the browser playing a video) —
   only that app's audio should be transcribed alongside the mic.
4. Turn the microphone **off** (allowed while a native source is armed) and
   record — transcript comes from the native source alone; no mic permission
   prompt appears on a fresh install in this mode.
5. Zoom two-device test: join a meeting from a second device, wear
   headphones on the Mac, arm "Zoom" + mic, record — both sides of the call
   land in the transcript as separate speakers.
6. Deny the TCC permission (System Settings) and start a capture — a toast
   explains where to re-enable it; with the mic still on, recording
   continues mic-only.
7. Quit the app mid-capture — no stray "Whisper Studio Capture" device stays
   behind (check Audio MIDI Setup).

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
  Whisper Studio menu for flows that need a real Chrome tab. Inside the
  shell, native system/app capture (see "Audio sources" above) covers the
  meeting use case instead, so the tab row is hidden there.
- The app icon is a plain solid-colour placeholder.
- Intel Macs are unsupported (arm64-only binaries throughout).
