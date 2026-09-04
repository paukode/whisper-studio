"""WHISPER.md project instructions, loaded as a tree rather than one flat file.

A repo's useful guidance is not uniform: the gotcha about the auth middleware
matters when touching `server/auth/`, and is noise on every other turn. So the
root WHISPER.md always loads (it is the project overview), while directory-scoped
WHISPER.md files load only when the turn actually concerns their directory.
Everything else is announced by path, for the model to read with ws_read_file if
it turns out to be relevant.

Both halves are capped, so one long WHISPER.md cannot silently tax every turn in
the session or crowd out the conversation itself.

AGENTS.md (the ecosystem-standard instructions filename other coding tools
maintain) is honored live as a read-only supplement, so a repo that keeps one
works here without a stale one-time import (server/onboarding/import_external
still converts it to WHISPER.md for users who want to edit in the app).
WHISPER.md always wins: at the root the AGENTS.md block is loaded alongside it
with an explicit precedence note (and skipped entirely when its text is already
verbatim inside WHISPER.md, the post-import state); in a scoped directory a
WHISPER.md shadows a sibling AGENTS.md outright.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("whisper-studio")

FILENAME = "WHISPER.md"
COMPAT_FILENAME = "AGENTS.md"

# The root file is the always-on overview, so it gets the larger budget.
ROOT_MAX_CHARS = 12_000
# A directory-scoped file only loads on turns about that directory.
SCOPED_MAX_CHARS = 6_000
# Depth and count bounds on discovery: this walk runs on every request, and a
# monorepo with hundreds of nested files should cost a bounded amount.
MAX_DEPTH = 4
MAX_SCOPED_FILES = 25


def _truncate(text: str, limit: int, rel_path: str) -> str:
    """Cap `text`, telling the model where to read the rest rather than hiding it."""
    if len(text) <= limit:
        return text
    return (
        text[:limit].rstrip() + f"\n\n[truncated at {limit} characters — read {rel_path} "
        "with ws_read_file for the rest]"
    )


def find_scoped_files(ws_path: str) -> list[str]:
    """Workspace-relative paths of every non-root WHISPER.md (or, in a
    directory without one, AGENTS.md), sorted, bounded.

    Skips the usual VCS/build/vendor directories, reusing the workspace's own
    ignore set so this agrees with what the file tools show. A WHISPER.md
    shadows a sibling AGENTS.md: one instructions file per directory, and the
    native one wins.
    """
    from server.workspace.paths import _WS_IGNORED_DIRS

    found: list[str] = []
    ws_path = os.path.abspath(ws_path)
    for dirpath, dirnames, filenames in os.walk(ws_path):
        rel_dir = os.path.relpath(dirpath, ws_path)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        if depth >= MAX_DEPTH:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(
            d for d in dirnames if d not in _WS_IGNORED_DIRS and not d.startswith(".")
        )
        if rel_dir != ".":
            if FILENAME in filenames:
                found.append(os.path.join(rel_dir, FILENAME))
            elif COMPAT_FILENAME in filenames:
                found.append(os.path.join(rel_dir, COMPAT_FILENAME))
            if len(found) >= MAX_SCOPED_FILES:
                break
    return sorted(found)


def _is_relevant(rel_path: str, question: str) -> bool:
    """Whether a scoped file's directory is plausibly what this turn is about.

    Matches the directory name or any of its path segments against the question
    text. Deliberately generous: a false positive costs a few hundred tokens on
    one turn, while a false negative means the model works in a directory whose
    documented gotchas it never saw.
    """
    if not question:
        return False
    haystack = question.lower()
    rel_dir = os.path.dirname(rel_path)
    if rel_dir.lower() in haystack:
        return True
    segments = [s for s in rel_dir.split(os.sep) if len(s) > 2]
    return any(seg.lower() in haystack for seg in segments)


def _read(path: str) -> str:
    try:
        with open(path, errors="replace") as f:
            return f.read().strip()
    except OSError as e:
        # Surface why custom context isn't applying — users were silently getting
        # no project memory when WHISPER.md had a permission issue.
        log.warning("%s read failed at %s: %s", FILENAME, path, e)
        return ""


def get_whisper_md_context(ws_path: str | None, question: str = "") -> str:
    """The WHISPER.md block for one request: root, relevant scoped files, index."""
    if not ws_path:
        return ""

    parts: list[str] = []

    root_content = ""
    root_path = os.path.join(ws_path, FILENAME)
    if os.path.isfile(root_path):
        root_content = _read(root_path)
    if root_content:
        parts.append(
            f"[{FILENAME} — project-specific instructions]\n"
            + _truncate(root_content, ROOT_MAX_CHARS, FILENAME)
        )

    compat_content = ""
    compat_path = os.path.join(ws_path, COMPAT_FILENAME)
    if os.path.isfile(compat_path):
        compat_content = _read(compat_path)
    # Post-import state: the importer copied AGENTS.md into WHISPER.md verbatim,
    # so loading both would tax every turn with the same text twice.
    if compat_content and compat_content in root_content:
        compat_content = ""
    if compat_content:
        if root_content:
            # A supplement next to a real WHISPER.md gets the scoped budget and
            # an explicit precedence rule (instruction beats ordering).
            parts.append(
                f"[{COMPAT_FILENAME} — project instructions maintained for other "
                f"coding tools; where this conflicts with {FILENAME} above, "
                f"{FILENAME} wins]\n" + _truncate(compat_content, SCOPED_MAX_CHARS, COMPAT_FILENAME)
            )
        else:
            parts.append(
                f"[{COMPAT_FILENAME} — project-specific instructions]\n"
                + _truncate(compat_content, ROOT_MAX_CHARS, COMPAT_FILENAME)
            )

    try:
        scoped = find_scoped_files(ws_path)
    except OSError as e:
        log.warning("%s discovery failed under %s: %s", FILENAME, ws_path, e)
        scoped = []

    inlined: list[str] = []
    deferred: list[str] = []
    for rel in scoped:
        if _is_relevant(rel, question):
            content = _read(os.path.join(ws_path, rel))
            if content:
                inlined.append(
                    f"[{rel} — instructions for {os.path.dirname(rel)}/]\n"
                    + _truncate(content, SCOPED_MAX_CHARS, rel)
                )
        else:
            deferred.append(rel)

    parts.extend(inlined)

    if deferred:
        listed = "\n".join(f"  {rel}" for rel in deferred)
        parts.append(
            "[Directory-scoped instructions not loaded this turn]\n"
            "These files carry rules for their own directory. Read one with "
            "ws_read_file before you change anything under it:\n" + listed
        )

    if not parts:
        return ""
    return "\n\n" + "\n\n".join(parts)
