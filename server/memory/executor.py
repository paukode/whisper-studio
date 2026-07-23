"""Memory tool executors — implementations for memory_read/write/list/delete.

Registered via @register_executor so they participate in the standard
executor metadata system (concurrent_safe, read_only, etc.).

Two-tier routing: every tool accepts an optional ``scope`` of "global" or
"project". Global operates on data/global_memory/ and needs no workspace;
project operates on data/memory/<slug>/ and requires an open workspace.
When scope is omitted:
  - writes route by type (user/feedback -> global, project/reference ->
    project when a workspace is open, else global),
  - reads/deletes search project first, then global,
  - list shows both tiers.
"""

import logging
import os
import tempfile
import threading

from server.executors import register_executor
from server.memory.memdir import (
    ENTRYPOINT_NAME,
    MEMORY_SCOPES,
    MEMORY_TYPES,
    SCOPE_GLOBAL,
    SCOPE_PROJECT,
    get_global_memory_dir,
    get_memory_dir,
    is_path_within_memory_dir,
    normalize_memory_filename,
    rebuild_index,
    resolve_memory_dir,
)
from server.memory.scan import build_manifest, scan_memory_files
from server.memory.secret_scanner import check_and_block
from server.workspace import get_workspace_path

log = logging.getLogger("whisper-studio")

# Serializes read-modify-write sequences on the shared stores. The server is a
# single process (executors run on its thread pool), so a process-level lock is
# sufficient; torn files are prevented separately by the atomic replace below.
_WRITE_LOCK = threading.Lock()

# Scope routing for writes when the caller does not pass one: portable facts
# about the user go global, repo-specific facts stay with the project.
_TYPE_DEFAULT_SCOPE = {
    "user": SCOPE_GLOBAL,
    "feedback": SCOPE_GLOBAL,
    "project": SCOPE_PROJECT,
    "reference": SCOPE_PROJECT,
}


def _atomic_write(abs_path: str, content: str) -> None:
    """Write via temp file + os.replace so readers never see a torn file."""
    directory = os.path.dirname(abs_path)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-memory-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, abs_path)
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _validate_scope(scope: str) -> str:
    """Return an error string for an invalid explicit scope, else empty."""
    if scope and scope not in MEMORY_SCOPES:
        return f"Error: scope must be one of {', '.join(sorted(MEMORY_SCOPES))}."
    return ""


def _resolve_in_dir(filename: str, memory_dir: str) -> tuple[str | None, str]:
    """Resolve filename inside one memory dir with traversal guard.

    Returns (abs_path, error). abs_path is None on error.
    """
    abs_path = os.path.normpath(os.path.join(memory_dir, filename))
    if not is_path_within_memory_dir(abs_path, memory_dir):
        return None, f"Path traversal blocked: {filename} resolves outside memory directory."
    return abs_path, ""


def _resolve_write_path(
    filename: str, scope: str, mem_type: str
) -> tuple[str | None, str | None, str, str]:
    """Resolve the target for a write. Returns (abs_path, memory_dir, scope, error)."""
    ws_path = get_workspace_path()

    if not scope:
        # Existing-file affinity first: an unscoped update to a file that
        # already lives in some tier must update it in place. Otherwise the
        # type default would e.g. write user_role.md to global while a stale
        # pre-two-tier copy stays in the project dir, shadowing every
        # unscoped read (reads search project first).
        existing_path, existing_scope, err = _locate_existing(filename, "")
        if err:
            # _locate_existing already prefixes "Error: "; strip it since the
            # caller adds its own.
            return None, None, "", err.removeprefix("Error: ")
        if existing_path:
            scope = existing_scope
        else:
            scope = _TYPE_DEFAULT_SCOPE.get(mem_type, SCOPE_PROJECT)
            # Project-tier default degrades to global when no workspace is
            # open, so plain-chat sessions still persist what they learn.
            if scope == SCOPE_PROJECT and not ws_path:
                scope = SCOPE_GLOBAL

    memory_dir, err = resolve_memory_dir(scope, ws_path)
    if err:
        return None, None, scope, err

    abs_path, err = _resolve_in_dir(filename, memory_dir)
    if err:
        return None, None, scope, err
    return abs_path, memory_dir, scope, ""


def _locate_existing(filename: str, scope: str) -> tuple[str | None, str, str]:
    """Find an existing memory file for read/delete.

    With an explicit scope, look only there. Without one, search project
    first (when a workspace is open), then global.
    Returns (abs_path, scope_found, error). abs_path None with empty error
    means "not found" (the caller phrases the message).
    """
    ws_path = get_workspace_path()

    if scope:
        memory_dir, err = resolve_memory_dir(scope, ws_path)
        if err:
            return None, scope, f"Error: {err}"
        abs_path, err = _resolve_in_dir(filename, memory_dir)
        if err:
            return None, scope, f"Error: {err}"
        return (abs_path if os.path.isfile(abs_path) else None), scope, ""

    candidates: list[tuple[str, str]] = []
    if ws_path:
        candidates.append((SCOPE_PROJECT, get_memory_dir(ws_path)))
    candidates.append((SCOPE_GLOBAL, get_global_memory_dir()))

    for cand_scope, memory_dir in candidates:
        abs_path, err = _resolve_in_dir(filename, memory_dir)
        if err:
            return None, cand_scope, f"Error: {err}"
        if os.path.isfile(abs_path):
            return abs_path, cand_scope, ""
    return None, "", ""


def _searched_tiers_label() -> str:
    return "project or global memory" if get_workspace_path() else "global memory"


@register_executor("memory_read", read_only=True, concurrent_safe=True)
def execute_memory_read(
    tool_input: dict, transcript: str = "", attachments: dict | None = None
) -> str:
    filename = tool_input.get("filename", "")
    scope = tool_input.get("scope", "")
    if not filename:
        return "Error: filename is required."
    if err := _validate_scope(scope):
        return err

    abs_path, found_scope, err = _locate_existing(filename, scope)
    if err:
        return err
    if not abs_path:
        where = scope if scope else _searched_tiers_label()
        return f"Memory file not found in {where}: {filename}"

    try:
        with open(abs_path, encoding="utf-8") as f:
            return f"[scope: {found_scope}]\n{f.read()}"
    except OSError as e:
        return f"Error reading {filename}: {e}"


@register_executor("memory_write", read_only=False, concurrent_safe=False)
def execute_memory_write(
    tool_input: dict, transcript: str = "", attachments: dict | None = None
) -> str:
    filename = tool_input.get("filename", "")
    name = tool_input.get("name", "")
    description = tool_input.get("description", "")
    mem_type = tool_input.get("type", "")
    content = tool_input.get("content", "")
    scope = tool_input.get("scope", "")

    if not filename:
        return "Error: filename is required."
    if not name:
        return "Error: name is required."
    if mem_type and mem_type not in MEMORY_TYPES:
        return f"Error: type must be one of {', '.join(sorted(MEMORY_TYPES))}."
    if err := _validate_scope(scope):
        return err

    # Keep the file recallable: force a .md suffix and refuse dotfiles, which
    # the scanner (scan_memory_files) would otherwise silently skip.
    filename, err = normalize_memory_filename(filename)
    if err:
        return f"Error: {err}"

    abs_path, memory_dir, scope, err = _resolve_write_path(filename, scope, mem_type)
    if err:
        return f"Error: {err}"

    # Prevent overwriting MEMORY.md directly (use memory_list to view index).
    # Case-insensitive: on APFS "memory.md" IS MEMORY.md, so a cased variant
    # would silently bypass the guard.
    if os.path.basename(abs_path).lower() == ENTRYPOINT_NAME.lower():
        return "Error: Cannot write MEMORY.md directly. It is managed automatically."

    # Secret scanning — always runs
    full_content = (
        f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{content}"
    )
    is_clean, redacted, findings = check_and_block(full_content)
    if not is_clean:
        rules = ", ".join(f["label"] for f in findings)
        return (
            f"BLOCKED: Content contains potential secrets ({rules}). "
            f"Secrets were redacted. Please remove sensitive data before saving to memory."
        )

    # Ensure parent directory exists (for nested paths like subdir/file.md)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)

    try:
        with _WRITE_LOCK:
            _atomic_write(abs_path, full_content)
            rebuild_index(memory_dir)
    except OSError as e:
        return f"Error writing {filename}: {e}"

    log.info("Memory written: %s (type=%s, scope=%s)", filename, mem_type, scope)
    return f"Memory file saved: {filename} [scope: {scope}]"


@register_executor("memory_list", read_only=True, concurrent_safe=True)
def execute_memory_list(
    tool_input: dict, transcript: str = "", attachments: dict | None = None
) -> str:
    from server.memory.memdir import load_memory_index

    ws_path = get_workspace_path()

    tiers: list[tuple[str, str]] = [(SCOPE_GLOBAL, get_global_memory_dir())]
    if ws_path:
        tiers.append((SCOPE_PROJECT, get_memory_dir(ws_path)))

    parts = []
    for scope, memory_dir in tiers:
        index_content = load_memory_index(memory_dir)
        files = scan_memory_files(memory_dir)
        manifest = build_manifest(files)

        section = [f"## {scope.capitalize()} memory ({len(files)} files)"]
        if index_content:
            section.append(f"### MEMORY.md Index\n{index_content}")
        section.append(manifest)
        parts.append("\n".join(section))

    if not ws_path:
        parts.append("(No workspace connected: project-tier memory is unavailable.)")

    return "\n\n".join(parts)


@register_executor("memory_delete", read_only=False, concurrent_safe=False, destructive=True)
def execute_memory_delete(
    tool_input: dict, transcript: str = "", attachments: dict | None = None
) -> str:
    filename = tool_input.get("filename", "")
    scope = tool_input.get("scope", "")
    if not filename:
        return "Error: filename is required."
    if err := _validate_scope(scope):
        return err

    if os.path.basename(os.path.normpath(filename)).lower() == ENTRYPOINT_NAME.lower():
        return "Error: Cannot delete MEMORY.md. Edit it instead."

    abs_path, found_scope, err = _locate_existing(filename, scope)
    if err:
        return err
    if not abs_path:
        where = scope if scope else _searched_tiers_label()
        return f"Memory file not found in {where}: {filename}"

    try:
        with _WRITE_LOCK:
            os.remove(abs_path)
            ws_path = get_workspace_path()
            deleted_dir, _ = resolve_memory_dir(found_scope, ws_path)
            if deleted_dir:
                rebuild_index(deleted_dir)
    except OSError as e:
        return f"Error deleting {filename}: {e}"

    log.info("Memory deleted: %s (scope=%s)", filename, found_scope)
    return f"Memory file deleted: {filename} [scope: {found_scope}]"
