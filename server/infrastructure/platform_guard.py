"""Refuse to run anywhere but macOS on Apple silicon (arm64).

Whisper Studio supports exactly one platform. The server (server/main.py) and
the launchd index worker (server/index/agent.py) call require_macos_arm64()
before they load anything platform specific, so an unsupported machine stops
at once with one clear message instead of failing later in the sandbox, a
bundled binary or a model load. An Intel Mac, and an x86_64 Python running
under Rosetta on Apple silicon, report x86_64 and are refused too.

Standard library only: this module is imported before any third-party package.
"""

import platform
import sys


def require_macos_arm64() -> None:
    """Raise SystemExit unless this process runs on macOS on Apple silicon."""
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise SystemExit(
            "Whisper Studio requires macOS on Apple silicon (arm64). "
            f"Detected: {platform.system()}/{platform.machine()}."
        )
