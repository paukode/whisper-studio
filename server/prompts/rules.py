"""The user's own instructions, kept under ``~/.whisper`` (user_root()).

Two kinds of text, at two levels, all of it loaded into the prompt:

- ``WHISPER.md``: a profile. Who the user is, their stack and tools, how they
  like answers written. Prose the user curates by hand (the assistant edits it
  only when asked). The global one lives at ``~/.whisper/WHISPER.md``, the
  project one at ``~/.whisper/rules/<project-slug>/WHISPER.md``.
- ``rules/``: hard rules the assistant must follow, one imperative line each.
  ``rules/global/*.md`` applies everywhere; ``rules/<project-slug>/*.md`` only
  in that workspace. The assistant appends here when the user says "remember
  this", "my rule is", "from now on".

The project slug is the one the memory store uses (``<basename>-<8 char
hash>``), so a project's rules, instructions and memories share a name.

This module holds the paths and the GLOBAL layer, which is workspace
independent and sits in the cached static prefix (``rules_block``). The
project layer is loaded by ``server/whisper_md.py`` into the per-workspace
section. Nothing in the connected workspace itself is read as instructions
any more, except a repo's ``AGENTS.md`` (other tools' file, read-only).

Each loader caches by the (path, mtime) set it read, so an edit applies on
the next request without a restart and an unchanged tree costs a few stats.
"""

from __future__ import annotations

import logging
import os
import threading

from server.infrastructure.paths import user_root

log = logging.getLogger("whisper-studio")

PROFILE_FILENAME = "WHISPER.md"
RULES_FILENAME = "rules.md"
GLOBAL_DIRNAME = "global"

# A rules file is capped like the project instructions: one runaway file
# must not tax every turn of every session.
MAX_FILE_CHARS = 12_000


# ── Paths ─────────────────────────────────────────────────────────────────


def rules_root() -> str:
    return os.path.join(user_root(), "rules")


def global_rules_dir() -> str:
    return os.path.join(rules_root(), GLOBAL_DIRNAME)


def global_profile_path() -> str:
    return os.path.join(user_root(), PROFILE_FILENAME)


def project_slug(ws_path: str) -> str:
    from server.memory.memdir import get_workspace_slug

    return get_workspace_slug(ws_path)


def project_rules_dir(ws_path: str) -> str:
    return os.path.join(rules_root(), project_slug(ws_path))


def project_profile_path(ws_path: str) -> str:
    return os.path.join(project_rules_dir(ws_path), PROFILE_FILENAME)


def rules_files(directory: str) -> list[str]:
    """The ``*.md`` rule files in ``directory``, sorted, ``rules.md`` first,
    the profile excluded (it is loaded as its own block)."""
    try:
        names = sorted(
            n
            for n in os.listdir(directory)
            if n.lower().endswith(".md") and n != PROFILE_FILENAME and not n.startswith(".")
        )
    except OSError:
        return []
    if RULES_FILENAME in names:
        names.remove(RULES_FILENAME)
        names.insert(0, RULES_FILENAME)
    return [os.path.join(directory, n) for n in names]


# ── Reading ───────────────────────────────────────────────────────────────


def read_capped(path: str, label: str) -> str:
    """The file's text, trimmed and capped, or '' when missing or unreadable.
    Surfaces a read failure in the log: a permission problem used to mean
    silently getting no instructions."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read().strip()
    except FileNotFoundError:
        return ""
    except OSError as e:
        log.warning("instructions: could not read %s: %s", path, e)
        return ""
    if len(text) <= MAX_FILE_CHARS:
        return text
    return (
        text[:MAX_FILE_CHARS].rstrip()
        + f"\n\n[truncated at {MAX_FILE_CHARS} characters; the rest of {label} was not loaded]"
    )


def _mtimes(paths: list[str]) -> tuple:
    out = []
    for p in paths:
        try:
            out.append((p, os.path.getmtime(p)))
        except OSError:
            out.append((p, None))
    return tuple(out)


_lock = threading.Lock()
_cache: dict = {"key": None, "text": ""}


def load_global_instructions() -> str:
    """The global profile and the global rules as prompt text, or ''."""
    profile = global_profile_path()
    files = rules_files(global_rules_dir())
    key = _mtimes([profile, *files])
    with _lock:
        if key == _cache["key"]:
            return _cache["text"]

    parts: list[str] = []
    text = read_capped(profile, "~/.whisper/WHISPER.md")
    if text:
        parts.append(f"[~/.whisper/{PROFILE_FILENAME}, about the user and how they work]\n{text}")
    for path in files:
        text = read_capped(path, os.path.basename(path))
        if text:
            parts.append(f"[rules/{GLOBAL_DIRNAME}/{os.path.basename(path)}, global rules]\n{text}")
    joined = "\n\n".join(parts)

    with _lock:
        _cache["key"] = key
        _cache["text"] = joined
    return joined


def rules_block() -> str:
    """The global layer as a prompt section, or '' when nothing is set.

    Carries its own leading blank line, like every other prompt section (the
    registry joins sections with ``"".join``).
    """
    text = load_global_instructions()
    if not text:
        return ""
    return (
        "\n\n## The user's instructions\n"
        "These are the user's own files. Follow the rules unless the user explicitly "
        "asks otherwise in their message.\n\n" + text
    )


def append_rules(prompt: str) -> str:
    """Append the global layer to a system prompt (no-op when nothing is set).

    ``rules_block`` supplies the separator, so this is a plain concatenation.
    Used by the prompts that are built outside the section registry (the
    on-device runtime, scheduled runs).
    """
    return f"{prompt}{rules_block()}"
