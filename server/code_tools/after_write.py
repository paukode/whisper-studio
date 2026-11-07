"""Fix and format the assistant's code right after a write, and tell only the
model what changed.

``after_write`` runs the workspace's own tools on a file the app just wrote:
ruff for Python (server/code_tools/ruff.py), Prettier and ESLint for JS/TS
inside the workspace (server/code_tools/js_format.py). It returns the note the
model reads with the write's result: the tools' findings and, when they
changed the file, that change itself, so the model's picture of the file
matches the disk without reading it again. The note never reaches the
activity row (ApprovalOutcome.model_note), which shows the write like any
other.
"""

import difflib
import os

from .commands import JS_EXTENSIONS, PYTHON_EXTENSIONS

# The change is shown as a diff when that is shorter than the file, else as
# the whole file; past this size neither is shown.
MAX_SHOWN_CHARS = 20_000


def handles(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in (*PYTHON_EXTENSIONS, *JS_EXTENSIONS)


def after_write(ws: str | None, full_path: str) -> str:
    """The model's note for the file the app just wrote to ``full_path``. A
    file outside the workspace ``ws`` is only checked by ruff, never fixed."""
    real = os.path.realpath(full_path)
    inside = bool(ws) and os.path.commonpath([real, os.path.realpath(ws)]) == os.path.realpath(ws)
    rel = os.path.relpath(real, os.path.realpath(ws)) if inside else os.path.basename(real)
    written = _text(real)
    if os.path.splitext(real)[1].lower() in PYTHON_EXTENSIONS:
        from . import ruff

        note = ruff.after_save(real, ws)
    elif inside:
        from . import js_format

        note = js_format.after_write(ws, real, rel)
    else:
        return ""
    now = _text(real)
    if written is not None and now is not None and now != written:
        change = _change_note(rel, written, now)
        note = f"{note}\n\n{change}" if note else change
    return note


def _text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _change_note(rel: str, written: str, now: str) -> str:
    head = (
        f"The project's formatter changed {rel} after your write, so the file is not "
        "exactly what you sent. Edits match against the file as it is now"
    )
    diff = "".join(
        difflib.unified_diff(
            written.splitlines(keepends=True),
            now.splitlines(keepends=True),
            "as you wrote it",
            "as it is now",
            n=2,
        )
    )
    if len(diff) < len(now) and len(diff) <= MAX_SHOWN_CHARS:
        return f"{head}; the change:\n```diff\n{diff.rstrip()}\n```"
    if len(now) <= MAX_SHOWN_CHARS:
        return f"{head}; the whole file now:\n```\n{now.rstrip()}\n```"
    return f"{head}. The change is too large to show here: read the part you edit next."
