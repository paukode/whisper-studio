Whisper Studio — read me first

1. Drag "Whisper Studio" onto the Applications folder in this window.

2. Open it from your Applications folder (not from this disk image).

First launch only: because this app is distributed independently and not
through the App Store, macOS may show a warning the first time you open it,
something like "Whisper Studio cannot be opened because the developer cannot
be verified," or "the app is damaged." This is expected. The app is safe; it
simply has not gone through Apple's paid notarization service. You only need
to clear this once.

To open it:

  Option A (no Terminal):
    - Double-click Whisper Studio. When the warning appears, click Cancel.
    - Open System Settings > Privacy & Security.
    - Scroll down. You'll see a line about Whisper Studio being blocked, with
      an "Open Anyway" button. Click it and confirm with your password/Touch ID.
    - Open Whisper Studio again. It will launch, and never warn you again.

  Option B (one Terminal command, most reliable):
    - Open the Terminal app.
    - Paste this line and press Return:

        xattr -dr com.apple.quarantine "/Applications/Whisper Studio.app"

    - Now open Whisper Studio normally.

Notes:
  - Requires an Apple Silicon Mac (M1 or newer) running macOS 14 or later.
  - On first recording, macOS will ask permission to use the microphone. Allow
    it; transcription runs entirely on your Mac.
  - Speech and other models download inside the app the first time you use a
    feature, so the first run needs an internet connection.

Enjoy.
