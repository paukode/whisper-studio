"""Guardrail: keep source files from re-growing into god-modules.

The big refactor split server/workspace.py (1932 lines), server/chat.py
(1344), ChatInput.tsx (2039) and useChatStream.ts (1217) into focused
modules. This test is the backstop that stops them — or any new file —
from creeping back over the line.

MAX_LINES is a deliberately loose ceiling (well above the current largest
file) so it only fires on genuine regressions, not on every edit. When a
file legitimately needs to exceed it, add it to ALLOWLIST with a reason
rather than raising the global limit — that keeps the pressure local.
"""

import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Backstop ceiling. The pre-refactor god-modules were ~1900-2000 lines;
# the largest file today is ~1120. 1200 blocks a relapse while leaving
# headroom for the current code.
MAX_LINES = 1200

# Files allowed to exceed MAX_LINES, each with a justification.
ALLOWLIST: dict[str, str] = {
    # The single coherent /api/chat state machine (turn loop, SSE streaming,
    # tool execution, approval pause/resume) — intentionally kept intact (see
    # the module docstring). On this branch it additionally carries on-device
    # model routing and cloud prompt-caching wiring. The cleanly separable
    # pieces are already extracted (server/local/route.py, server/chat/caching.py);
    # the remainder is the streaming state machine, which splitting would only
    # obscure.
    "server/chat/routes.py": "Monolithic /api/chat streaming state machine + local routing + caching; cleanly separable parts already extracted.",
}

# Directories we never scan (vendored, generated, virtualenvs).
_SKIP_DIRS = {
    "node_modules",
    "dist",
    "build",
    "static",
    ".git",
    "venv",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}


def _iter_source_files():
    for base, exts in (("server", (".py",)), ("src", (".ts", ".tsx"))):
        root = os.path.join(REPO_ROOT, base)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in filenames:
                if not name.endswith(exts):
                    continue
                # Tests and type-declaration files are exempt — they have
                # their own size dynamics and aren't shipped logic.
                if ".test." in name or name.endswith(".d.ts"):
                    continue
                yield os.path.join(dirpath, name)


def test_no_source_file_exceeds_line_budget():
    offenders = []
    for path in _iter_source_files():
        rel = os.path.relpath(path, REPO_ROOT)
        if rel in ALLOWLIST:
            continue
        with open(path, errors="replace") as f:
            n = sum(1 for _ in f)
        if n > MAX_LINES:
            offenders.append(f"{rel}: {n} lines (> {MAX_LINES})")
    assert not offenders, (
        "These files exceed the "
        f"{MAX_LINES}-line budget — split them into a package, or add an "
        "entry to ALLOWLIST in this test with a justification:\n  " + "\n  ".join(sorted(offenders))
    )


def test_allowlisted_files_still_exist():
    """An allowlist entry that no longer points at a real file is stale —
    fail so it gets cleaned up rather than silently masking a future file
    of the same name."""
    missing = [p for p in ALLOWLIST if not os.path.isfile(os.path.join(REPO_ROOT, p))]
    assert not missing, f"Stale ALLOWLIST entries (file gone): {missing}"
