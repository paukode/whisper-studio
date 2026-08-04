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
- **System audio** — everything the Mac plays. EVERY process belonging to
  Whisper Studio itself (shell, its WebKit GPU/media helpers, the spawned
  backend tree) is excluded from the tap.
- **One app** (e.g. Zoom) — the per-app list shows USER-FACING APPS currently
  producing audio and refreshes each time the menu opens. Helper processes
  ("Google Chrome Helper", WebKit XPC services) are grouped into their app;
  one row can span several audio pids, and starting a capture re-resolves and
  taps all of them. Processes with no .app ancestor keep their process name
  (e.g. `afplay`). Whisper Studio itself never appears in the list.

While recording, the active native source row shows a small 3-bar activity
meter (driven by the live capture level), and the header's source label gets
a green dot while the native capture is audibly delivering sound. If a
capture starts but stays silent for 5 seconds (no callbacks, or only zero
samples — the classic symptom of a revoked/denied System Audio Recording
permission), a warning toast explains where to fix it; it persists to the
notification bell.

The selection persists across launches. All sources are mixed into ONE
16 kHz mono stream in the frontend, so the backend websocket contract is
unchanged and speaker diarization separates the voices. Wear headphones so
captured playback is not picked up a second time by the mic.

Implementation: Core Audio process taps (`CATapDescription` +
`AudioHardwareCreateProcessTap` + a private TAP-ONLY aggregate device +
IOProc) in `macapp/shell/NativeAudioCapture.swift`, converted to 16 kHz mono
Int16 with `AVAudioConverter` and pushed to the SPA over a WKWebView bridge
(`window.webkit.messageHandlers.nativeAudio` /
`window.__whisperNativeAudio`). The frontend wrapper is
`src/services/nativeAudioSource.ts`.

The aggregate device contains NO physical sub-devices: including the real
output device opens an IO path to the user's speakers/headphones (Bluetooth
headsets flip into call mode and the app shows up in the audio chain). Only
if the tap-only aggregate fails does the shell fall back to including the
default output device — and then every output buffer is zero-filled in the
IOProc and the tap's input buffers are located by format rather than assumed
at index 0. The default input/output devices are never modified.

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
/tmp/smoke_list_sources            # JSON: System audio + grouped app rows
/tmp/smoke_list_sources --assert   # grouping / self-exclusion assertions
```

With `afplay` playing, the list must contain an `afplay` row (process name,
no bundle); with Chrome playing, ONE "Google Chrome" row (helpers grouped),
never a bare "helper" row; and never any Whisper Studio row.

**Manual test checklist**

1. Build and launch the app, play music (Music/Safari/`afplay`), open the
   headphones menu → the playing app appears in "This Mac" under its real
   app name. Chrome shows as ONE "Google Chrome" row (no "helper" rows), and
   no "Whisper Studio"/"Whisper Studio Graphics and Media" row ever appears.
2. Pick **System audio**, press Record — first time, expect the OS
   permission prompt; accept. Speak AND keep the music playing: the
   transcript should contain the music's lyrics/speech and your voice, with
   diarization splitting speakers. The header shows "Mic + System audio"
   with a green live dot while captured audio is audible, and the source
   menu's active row shows a moving 3-bar level meter.
3. While recording with headphones on, playback must stay untouched: no
   device switch in System Settings > Sound, no Bluetooth headset dropping
   into call-quality audio, no audio "routing through" Whisper Studio
   (the aggregate is tap-only; check `backend.log` for "tap-only aggregate"
   in the `[native-audio]` capture-started line).
4. Stop. Pick the app source instead (e.g. the browser playing a video) —
   only that app's audio should be transcribed alongside the mic, including
   audio played by the app's helper processes (a second Chrome tab/window).
5. Turn the microphone **off** (allowed while a native source is armed) and
   record — transcript comes from the native source alone; no mic permission
   prompt appears on a fresh install in this mode.
6. Zoom two-device test: join a meeting from a second device, wear
   headphones on the Mac, arm "Zoom" + mic, record — both sides of the call
   land in the transcript as separate speakers.
7. Deny the TCC permission (System Settings) and start a capture — either
   the start fails with a toast pointing at System Settings, or (macOS
   versions where a denied tap starts silently) a warning toast appears
   after ~5 s: "System audio capture is producing no sound…" — and it lands
   in the notification bell. The MIC KEEPS RECORDING either way: with the
   mic on, the transcript keeps growing mic-only while the native side is
   silent or degraded.
8. Quit the app mid-capture — no stray "Whisper Studio Capture" device stays
   behind (check Audio MIDI Setup).

## Downloads (exports)

WKWebView has no default download behavior — without explicit handling, an
export click would just navigate the web view to the generated blob and
render raw markdown over the app. The shell therefore routes downloads
natively:

- Every export in the SPA (chat/transcript Export buttons, sidebar session
  export menu, message export, costs export, HTML artifact downloads,
  `/export`) goes through one shared helper, `src/utils/downloadFile.ts`,
  which clicks a temporary anchor with the `download` attribute. In
  browsers that downloads normally; in the shell it marks the navigation
  action `shouldPerformDownload`.
- `main.swift` turns such navigation actions — and any response with an
  unrenderable MIME type or a `Content-Disposition: attachment` header —
  into a `WKDownload`, delegated to `macapp/shell/DownloadHandler.swift`.
- Files are saved into `~/Downloads` with the suggested filename, never
  overwriting: collisions get `name (2).ext`, `name (3).ext`, … suffixes.
  No save panel is shown.
- On completion the shell calls the page hook `window.__whisperShellToast`
  (registered in `src/services/shellToastBridge.ts`): a "Saved to
  Downloads: <file>" toast appears and persists to the notification bell
  (source "export"). Failures raise an error toast with the reason. If the
  hook is missing (older cached page), the shell logs to stderr and stays
  silent — no alert, no crash.

**Permission**: the first write into `~/Downloads` triggers the standard
macOS "wants to access your Downloads folder" consent prompt. For a
non-sandboxed app this needs no Info.plist usage key and no entitlement —
the TCC prompt is automatic (there is no purpose-string key for the
Downloads folder; sandboxed apps would instead need the
`com.apple.security.files.downloads.read-write` entitlement, which does not
apply here). If access was denied, re-enable it under System Settings >
Privacy & Security > Files & Folders.

**Dedupe smoke test (no app launch needed)**

```bash
swiftc -D DOWNLOAD_SMOKE_CLI -parse-as-library \
    -target arm64-apple-macos14 \
    macapp/shell/DownloadHandler.swift -o /tmp/smoke_download \
    -framework WebKit
/tmp/smoke_download   # destination-dedupe + JS-escaping assertions
```

**Manual test checklist**

1. Open a chat with messages and click Export — first time, expect the OS
   "access your Downloads folder" prompt; accept. The file (e.g.
   `conversation-<session>.md`) lands in `~/Downloads`, and the app UI
   stays put (no raw markdown page).
2. Click Export again — a second file appears with a
   ` (2)` suffix (`conversation-<session> (2).md`); nothing is overwritten.
3. A "Saved to Downloads: <filename>" toast appears for each export and the
   messages are kept in the header notification bell.

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
