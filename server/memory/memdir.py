"""Memory directory management — MEMORY.md loading, truncation, path resolution.

Two-tier layout:
    data/global_memory/            — global tier: cross-workspace facts, alive
        MEMORY.md                    even in plain chat with no workspace open
        *.md                       — topic files with YAML frontmatter
        .cursor.json               — extraction cursor + throttle state
    data/memory/<workspace_slug>/  — project tier: scoped to one workspace
        MEMORY.md                  — index file (max 200 lines, 25 KB)
        *.md
        .cursor.json
        .dream_meta.json           — dream consolidation metadata

data/global_memory/ deliberately lives OUTSIDE data/memory/ so it can never
collide with a workspace slug (slugs sanitize to [a-zA-Z0-9._-], so a marker
folder like "_global" under data/memory/ would clash with a workspace that is
actually named "global").
"""

import hashlib
import logging
import os
import re

from server.infrastructure.paths import data_root

log = logging.getLogger("whisper-studio")

MEMORY_BASE = os.path.join(data_root(), "memory")
GLOBAL_MEMORY_DIR = os.path.join(data_root(), "global_memory")

ENTRYPOINT_NAME = "MEMORY.md"
MAX_ENTRYPOINT_LINES = 200
MAX_ENTRYPOINT_BYTES = 25_000

# Valid memory types (closed taxonomy)
MEMORY_TYPES = frozenset({"user", "feedback", "project", "reference"})

# Memory scopes (two-tier store)
SCOPE_GLOBAL = "global"
SCOPE_PROJECT = "project"
MEMORY_SCOPES = frozenset({SCOPE_GLOBAL, SCOPE_PROJECT})


def _sanitize_basename(ws_path: str) -> str:
    """Directory-safe basename of a workspace path (no realpath disambiguation)."""
    name = os.path.basename(os.path.normpath(ws_path))
    return re.sub(r"[^a-zA-Z0-9._-]", "_", name).strip("_") or "default"


def get_workspace_slug(ws_path: str) -> str:
    """Sanitize a workspace path into a directory-safe slug.

    The slug is the sanitized basename plus a short hash of the workspace's
    full realpath. The hash disambiguates two different workspaces that share a
    basename (e.g. ~/a/app and ~/b/app): basename-only slugs made them collide
    on one project-memory dir and cross-leak each other's memories.
    """
    base = _sanitize_basename(ws_path)
    digest = hashlib.sha1(os.path.realpath(ws_path).encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"


def _migrate_legacy_slug_dir(ws_path: str, new_dir: str) -> None:
    """One-time rename of a pre-disambiguation (basename-only) memory dir to the
    new realpath-hashed slug.

    Only runs when unambiguous: the legacy dir exists AND the new dir does not.
    The old scheme kept exactly one dir per basename, so the first workspace
    with that basename to access memory after the upgrade adopts it, keeping a
    single-occupant workspace's memories intact. Refusing to act when the new
    dir already exists means we never clobber an already-migrated (or freshly
    created) store. Best-effort: a failed rename must not break memory access.
    """
    legacy_slug = _sanitize_basename(ws_path)
    legacy_dir = os.path.join(MEMORY_BASE, legacy_slug)
    if legacy_dir == new_dir:
        return  # the hash suffix makes these differ; guard against edge cases
    if os.path.isdir(legacy_dir) and not os.path.exists(new_dir):
        try:
            os.replace(legacy_dir, new_dir)
            log.info("Migrated legacy memory dir %s -> %s", legacy_slug, os.path.basename(new_dir))
        except OSError as e:
            log.warning("Failed to migrate legacy memory dir %s: %s", legacy_slug, e)


def get_memory_dir(ws_path: str | None = None) -> str:
    """Return the memory directory for a workspace. Creates it if needed."""
    slug = get_workspace_slug(ws_path) if ws_path else "default"
    mem_dir = os.path.join(MEMORY_BASE, slug)
    if ws_path:
        os.makedirs(MEMORY_BASE, exist_ok=True)
        _migrate_legacy_slug_dir(ws_path, mem_dir)
    os.makedirs(mem_dir, exist_ok=True)
    return mem_dir


def get_global_memory_dir() -> str:
    """Return the global (cross-workspace) memory directory. Creates it if needed."""
    os.makedirs(GLOBAL_MEMORY_DIR, exist_ok=True)
    return GLOBAL_MEMORY_DIR


def ensure_memory_dir(ws_path: str | None) -> str | None:
    """Guard: return project memory dir only if a workspace exists and auto_memory is on."""
    from server.infrastructure.feature_flags import is_enabled

    if not ws_path or not is_enabled("auto_memory"):
        return None
    return get_memory_dir(ws_path)


def ensure_global_memory_dir() -> str | None:
    """Guard: return the global memory dir if auto_memory is enabled.

    Unlike the project tier, this needs no workspace — global memory is alive
    in plain chat mode too.
    """
    from server.infrastructure.feature_flags import is_enabled

    if not is_enabled("auto_memory"):
        return None
    return get_global_memory_dir()


def resolve_memory_dir(scope: str, ws_path: str | None) -> tuple[str | None, str]:
    """Resolve a scope name to its memory directory.

    Returns (memory_dir, error). memory_dir is None when the scope cannot be
    resolved; error then holds a human-readable reason.
    """
    if scope == SCOPE_GLOBAL:
        return get_global_memory_dir(), ""
    if scope == SCOPE_PROJECT:
        if not ws_path:
            return None, (
                "No workspace connected. Open a workspace for project memory, "
                "or use scope='global'."
            )
        return get_memory_dir(ws_path), ""
    return None, f"Unknown memory scope: {scope!r}. Use one of {sorted(MEMORY_SCOPES)}."


def load_memory_index(memory_dir: str) -> str:
    """Read MEMORY.md with line/byte truncation. Returns content or empty string."""
    path = os.path.join(memory_dir, ENTRYPOINT_NAME)
    if not os.path.isfile(path):
        return ""

    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        log.warning("Failed to read %s: %s", path, e)
        return ""

    trimmed = raw.strip()
    if not trimmed:
        return ""

    lines = trimmed.split("\n")
    line_count = len(lines)
    byte_count = len(trimmed.encode("utf-8"))

    was_line_truncated = line_count > MAX_ENTRYPOINT_LINES
    was_byte_truncated = byte_count > MAX_ENTRYPOINT_BYTES

    if not was_line_truncated and not was_byte_truncated:
        return trimmed

    # Line-truncate first
    content = "\n".join(lines[:MAX_ENTRYPOINT_LINES]) if was_line_truncated else trimmed

    # Then byte-truncate. Slice on ENCODED BYTES, not characters: content[:N]
    # counts codepoints, so for multibyte text (accents, CJK, emoji) it can still
    # leave the body above the byte cap, or a raw byte slice could split a
    # codepoint. Encode, cut at the byte limit, then decode with errors="ignore"
    # to drop any partial trailing codepoint; the result is valid UTF-8 and its
    # encoding is guaranteed <= the cap. Finally trim back to the last newline so
    # the truncation lands on a line boundary.
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_ENTRYPOINT_BYTES:
        content = encoded[:MAX_ENTRYPOINT_BYTES].decode("utf-8", errors="ignore")
        cut = content.rfind("\n")
        if cut > 0:
            content = content[:cut]

    # Build warning
    if was_byte_truncated and not was_line_truncated:
        reason = f"{byte_count} bytes (limit: {MAX_ENTRYPOINT_BYTES})"
    elif was_line_truncated and not was_byte_truncated:
        reason = f"{line_count} lines (limit: {MAX_ENTRYPOINT_LINES})"
    else:
        reason = f"{line_count} lines and {byte_count} bytes"

    content += (
        f"\n\n> WARNING: MEMORY.md is {reason}. Only part of it was loaded. "
        "Keep index entries to one line under ~200 chars; move detail into topic files."
    )
    return content


def is_path_within_memory_dir(filepath: str, memory_dir: str) -> bool:
    """Check that a resolved path stays within the memory directory."""
    real_path = os.path.realpath(filepath)
    real_dir = os.path.realpath(memory_dir)
    return real_path.startswith(real_dir + os.sep) or real_path == real_dir


def normalize_memory_filename(filename: str) -> tuple[str, str]:
    """Normalize a memory filename to the scanner's contract, or reject it.

    ``scan_memory_files`` only surfaces non-dot files ending in ``.md``; a
    filename that misses either rule creates a memory that can never be
    recalled or listed. So here we:
      - append ``.md`` when it is missing (case-insensitive, so ``FOO.MD`` and
        cased ``MEMORY.MD`` variants are left for the entrypoint guard), and
      - reject a basename that starts with ``.`` (a dotfile the scanner skips).

    Returns ``(normalized_filename, error)``. On rejection the error is
    non-empty and the filename is empty.
    """
    if not filename:
        return "", "filename is required."
    if os.path.basename(filename).startswith("."):
        return "", "memory filenames must not start with '.' (dotfiles are not recallable)."
    if not filename.lower().endswith(".md"):
        filename = f"{filename}.md"
    return filename, ""


def rebuild_index(memory_dir: str) -> None:
    """Regenerate MEMORY.md from topic-file frontmatter.

    Deterministic (no LLM): one pointer line per topic file, newest first,
    capped at the entrypoint line limit. Called after every memory write or
    delete, so the index can never drift from the store. Direct writes to
    MEMORY.md stay blocked in the executor; this is the only writer.
    Best-effort: an unwritable index must never fail the memory operation.
    """
    from server.memory.scan import scan_memory_files

    if not os.path.isdir(memory_dir):
        return

    lines = []
    for m in scan_memory_files(memory_dir):
        tag = f"[{m.type}] " if m.type else ""
        desc = (m.description or "").strip()
        # One line per entry; keep the index lean
        if len(desc) > 150:
            desc = desc[:147] + "..."
        suffix = f": {desc}" if desc else ""
        lines.append(f"- {tag}[{m.name}]({m.filename}){suffix}")

    # Leave room for the header lines within the entrypoint budget
    lines = lines[: MAX_ENTRYPOINT_LINES - 4]

    path = os.path.join(memory_dir, ENTRYPOINT_NAME)
    if not lines:
        # Empty store: drop the index rather than leaving a stale one
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("Failed to remove empty memory index %s: %s", path, e)
        return

    content = (
        "# Memory index\n\n"
        "<!-- Auto-generated from topic-file frontmatter; do not edit by hand. -->\n\n"
        + "\n".join(lines)
        + "\n"
    )
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except OSError as e:
        log.warning("Failed to rebuild memory index %s: %s", path, e)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
