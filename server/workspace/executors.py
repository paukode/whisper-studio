"""All `@register_executor` handlers for workspace tools.

Imports from sibling modules (paths/state/filesystem/commands) — no FastAPI
or route deps. The decorators fire at import time, so the package `__init__.py`
must import this module for tool registration to take effect.

Also home to:
- execute_ws_open_folder: the public bridge used by chat.py / tool_router.py
- _normalize_quotes + _replace_with_normalization: quote-tolerant matching
  used by ws_edit_file
- _WORKTREES: in-memory registry shared with the worktree route handlers

ws_run_command asks read_only._is_read_only_command whether a command may skip
the approval card; one that does runs under the read-only OS sandbox.
"""

import base64
import json
import logging
import os
import subprocess
from dataclasses import dataclass

from server import file_state
from server.approval.markers import approval_request
from server.executors import register_executor
from server.security import secret_reads as secret_reads_policy

from .commands import (
    _apply_stdin_redirect,
    _interpret_exit_code,
    _is_silent_command,
    _needs_stdin_redirect,
    _truncate_shell_output,
    _validate_command,
)
from .filesystem import _ws_list_dir
from .paths import (
    _WS_BINARY_EXTS,
    WORKSPACE_BACKUPS,
    _is_system_path,
    _normalize_lf,
    _ws_path_allowed,
    in_other_scratch,
    own_scratch_dir,
    stage_file_bytes,
)
from .read_only import _is_read_only_command
from .state import (
    CONFIG_LOCK,
    _workspace_prompt_payload,
    get_workspace_path,
    load_workspace_config,
    resolve_write_destination,
    save_recent_workspace,
    save_workspace_config,
    workspace_lost_message,
)

log = logging.getLogger("whisper-studio")


def outside_roots_error(path: str, ws: str, session_id: str) -> str:
    """The refusal for a ws_* file tool aimed at changing a file outside both
    places it may change.

    A plain tool result, never the folder picker: the workspace the user
    connected is not in question, only the path is, and the model can act on
    this on its own (a path inside the workspace or its own scratch folder, a
    save the user approves, or terminal_run's escalation) without pausing the
    turn. Reading is a different matter: ws_read_file and ws_list_directory
    read anywhere but other sessions' scratch folders (see resolve_read_path)."""
    if in_other_scratch(os.path.join(ws, path), session_id):
        return other_scratch_error(path, session_id)
    scratch = own_scratch_dir(session_id)
    places = f"this session's scratch folder ({scratch})" if scratch else "a scratch folder"
    return (
        f"Error: {path} is outside the connected workspace ({ws}) and outside "
        f"{places}. The ws_* file tools "
        "change files only in those two places (reading works anywhere). To write "
        "elsewhere, pass the absolute path to ws_create_file or save_file, which asks "
        "the user to approve the save, or use terminal_run with "
        "sandbox_permissions='danger-full-access'."
    )


def other_scratch_error(path: str, session_id: str) -> str:
    """The refusal for a ws_* file tool aimed at another session's scratch
    folder: a session works only in its own (the user's rule)."""
    return (
        f"Error: {path} is in another session's scratch folder. This session reads "
        f"and changes files only in its own scratch folder ({own_scratch_dir(session_id)})."
    )


# resolve_read_path's refusal for a secret path the call may not read without
# a person's yes: the read tool turns it into a secret_read card.
SECRET_READ_NEEDED = "secret read needs approval"


def resolve_read_path(
    path: str, ws: str | None, session_id: str, secret_reads: bool = False
) -> tuple[str | None, str | None]:
    """Where a ws_* read tool reads from: ``(full path, refusal)``.

    Reading never asks and is allowed anywhere on this machine (the user's
    rule), so an absolute path may point outside the connected workspace and
    the scratch folder, with or without a workspace connected; a relative
    path needs the workspace to resolve against. The exceptions are other
    sessions' scratch folders (a session works only in its own) and the kernel
    pseudo-filesystems, refused with a plain error, and the credential stores
    (server/security/sensitive_paths.py): read with ``secret_reads`` (Bypass,
    or a person's yes), else refused with SECRET_READ_NEEDED so the tool asks
    (server/security/secret_reads.py)."""
    raw = os.path.expanduser((path or "").strip())
    if os.path.isabs(raw):
        full = raw
    elif ws:
        full = os.path.join(ws, raw) if raw and raw != "." else ws
    else:
        return None, "No workspace connected."
    if in_other_scratch(full, session_id):
        return None, other_scratch_error(path, session_id)
    if ws and _ws_path_allowed(full, ws, session_id):
        return full, None
    if _is_system_path(full):
        return None, f"Error: {path} is not a readable location."
    from server.security.sensitive_paths import is_sensitive_path

    if is_sensitive_path(full) and not secret_reads:
        return None, SECRET_READ_NEEDED
    return full, None


def open_folder_now(path: str) -> str:
    """Create the folder if needed, connect it, and return connection info.

    The part of ws_open_folder that actually DOES the thing, split out so the
    approval executor (server/approval/bootstrap.py::_do_folder_access) can
    complete the very action the user approved instead of only recording the
    permission for it. ``path`` must already be expanded and realpath'd.

    Refused in a turn whose workspace the user let go of (see
    connect_workspace): reconnecting that folder would undo their disconnect.
    """
    lost = workspace_lost_message()
    if lost:
        return json.dumps({"error": lost})
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as e:
        return json.dumps({"error": f"Could not create folder: {e}"})
    # Connect workspace (serialized with the connect/disconnect handlers)
    with CONFIG_LOCK:
        config = load_workspace_config()
        config["path"] = path
        config.pop("mode", None)  # retired field; scrub it from old configs
        save_workspace_config(config)
    save_recent_workspace(path)
    WORKSPACE_BACKUPS.clear()
    entries = _ws_list_dir(path)
    return json.dumps(
        {
            "opened": True,
            "path": path,
            "files": len(entries),
            "__ws_switch__": path,  # Signal frontend to switch workspace view
        }
    )


def execute_ws_open_folder(tool_input: dict) -> str:
    """Resolve the folder the user named, connect it as the workspace (creating
    it only when asked), and return connection info.

    Resolution (server/workspace/folder_lookup.py): an existing path wins; a
    bare or misspelled name is matched against its parent, the usual roots and
    recent workspaces. One confident match proceeds; several come back as
    ``candidates`` for the model to confirm with the user; none comes back with
    the folders that DO exist there (``available``) so the model asks with real
    names instead of inventing some. Nothing is ever created unless
    ``create=true`` was passed: a misheard or mistyped name must not turn into
    an approval card for a folder nobody asked for.
    """
    raw = str(tool_input.get("path", "")).strip()
    if not raw:
        return json.dumps({"error": "path is required"})
    # The user disconnected (or switched away from) this turn's workspace
    # mid-turn. Opening a folder now, silently when it is under a grant, would
    # undo that, so the turn tells the user instead.
    lost = workspace_lost_message()
    if lost:
        return json.dumps({"error": lost})
    switch = _flag(tool_input.get("switch"))
    create = _flag(tool_input.get("create"))

    from server.workspace.folder_lookup import browse_names, resolve_folder
    from server.workspace.state import load_recent_workspaces

    recents = load_recent_workspaces()
    resolved, candidates = resolve_folder(raw, recents=recents)
    if resolved is None:
        if candidates:
            return json.dumps(
                {
                    "error": f"No folder is named exactly {raw!r}, but these are close.",
                    "candidates": candidates,
                    "hint": (
                        "Ask the user which one they meant (ask_user_question, options = these "
                        "exact names), then call ws_open_folder again with that path. Never "
                        "offer names that are not in this list. Pass create=true only if the "
                        "user asks for a new folder."
                    ),
                }
            )
        if not create:
            return json.dumps(
                {
                    "error": f"No folder matching {raw!r} exists in the usual places.",
                    "available": browse_names(raw, recents=recents),
                    "hint": (
                        "The name was probably misheard or mistyped. Ask the user which of the "
                        "available folders they meant (ask_user_question with those exact "
                        "names, or the closest few), or for the full path. Never invent folder "
                        "names, and never create a folder unless the user asked for a new one "
                        "(then pass create=true with the full path)."
                    ),
                }
            )
        path = os.path.realpath(os.path.expanduser(raw))
    else:
        path = resolved

    existing = get_workspace_path()
    if existing and os.path.realpath(existing) != path and not switch:
        return json.dumps(
            {
                "error": (
                    f"A workspace is already connected at '{existing}'. Use ws_read_file, "
                    "ws_write_file, and ws_create_file to work with it. If the user explicitly "
                    "asked to open or switch to this other folder, call ws_open_folder again "
                    "with switch=true."
                ),
                "resolved_path": path,
            }
        )
    # Ask the user once before reaching into a folder they have not already
    # approved, then remember the answer forever (folder_grants). Approving
    # the card OPENS the folder as well as recording the grant — the user
    # approved "open this folder", so granting without opening would leave
    # them staring at a still-disconnected workspace. From then on this check
    # passes silently, including for every folder beneath the granted one.
    from server.security.folder_grants import needs_grant

    if needs_grant(path, existing):
        payload = json.dumps(
            {
                "action": "folder_access",
                "path": path,
                "reason": "open this folder as the workspace",
                # The approval executor finishes the job: create the folder if
                # it does not exist yet (ws_open_folder has always been allowed
                # to), then connect it.
                "create": True,
                "connect": True,
            }
        )
        return approval_request(payload)
    return open_folder_now(path)


def _flag(value) -> bool:
    """Tool inputs arrive as JSON booleans, but models also send "true"."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("true", "yes", "1")


# --- Workspace Tool Executors ---


@register_executor("ws_read_file", read_only=True, concurrent_safe=True)
def _exec_ws_read_file(tool_input, transcript, current_attachments):
    ws = get_workspace_path()
    session_id = tool_input.pop("__session_id__", "")
    path = tool_input.get("path", "")
    full, refusal = resolve_read_path(
        path, ws, session_id, secret_reads=secret_reads_policy.stamped(tool_input)
    )
    if refusal == SECRET_READ_NEEDED:
        return secret_reads_policy.read_request("ws_read_file", tool_input, session_id)
    if refusal:
        return refusal
    if not os.path.isfile(full):
        return f"File not found: {path}"
    ext = os.path.splitext(path)[1].lower()
    if ext in _WS_BINARY_EXTS:
        return f"Binary file: {path} ({os.path.getsize(full)} bytes)"
    offset = tool_input.get("offset", 1)
    limit = tool_input.get("limit")
    # Dedup: return stub if same file read with same params and unchanged
    stub = file_state.check_dedup(session_id, full, offset, limit)
    if stub:
        return stub
    try:
        with open(full, errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return f"Error reading: {e}"
    total_lines = len(lines)
    if offset and offset > 1:
        lines = lines[offset - 1 :]
    if limit:
        lines = lines[:limit]
    numbered = []
    start = max(offset or 1, 1)
    for i, line in enumerate(lines):
        numbered.append(f"{start + i:>5}\t{line.rstrip()}")
    result = "\n".join(numbered)

    # Token gating: cap at ~25k tokens (~100k chars).
    # For files over budget, return head + tail so the LLM sees both
    # the beginning and end of the file, with a note about the gap.
    TOKEN_BUDGET_CHARS = 100_000
    if len(result) > TOKEN_BUDGET_CHARS:
        result_lines = numbered
        head_budget = TOKEN_BUDGET_CHARS // 2
        tail_budget = TOKEN_BUDGET_CHARS // 2
        # Find how many lines fit in each half
        head_lines = []
        head_chars = 0
        for line in result_lines:
            if head_chars + len(line) + 1 > head_budget:
                break
            head_lines.append(line)
            head_chars += len(line) + 1
        tail_lines = []
        tail_chars = 0
        for line in reversed(result_lines):
            if tail_chars + len(line) + 1 > tail_budget:
                break
            tail_lines.insert(0, line)
            tail_chars += len(line) + 1
        skipped = len(result_lines) - len(head_lines) - len(tail_lines)
        if skipped > 0:
            result = "\n".join(head_lines)
            result += f"\n\n... [{skipped} lines omitted — file exceeds token budget ({total_lines} total lines)] ...\n\n"
            result += "\n".join(tail_lines)
            result += (
                f"\n\n(Token budget: showing first {len(head_lines)} + last {len(tail_lines)} lines. "
                f"Use offset/limit to read specific sections.)"
            )
        # Fall through — result is now within budget

    file_state.record_read(session_id, full, offset, limit, len(numbered))
    return result


@register_executor("ws_list_directory", read_only=True, concurrent_safe=True)
def _exec_ws_list_dir(tool_input, transcript, current_attachments):
    ws = get_workspace_path()
    session_id = tool_input.pop("__session_id__", "")
    path = tool_input.get("path", "").strip() or "."
    full, refusal = resolve_read_path(
        path, ws, session_id, secret_reads=secret_reads_policy.stamped(tool_input)
    )
    if refusal == SECRET_READ_NEEDED:
        return secret_reads_policy.read_request("ws_list_directory", tool_input, session_id)
    if refusal:
        return refusal
    if not os.path.isdir(full):
        return f"Not a directory: {path}"
    entries = []
    try:
        from .paths import _WS_IGNORED_DIRS

        for name in sorted(os.listdir(full)):
            if name.startswith(".") or name in _WS_IGNORED_DIRS:
                continue
            fp = os.path.join(full, name)
            if os.path.isdir(fp):
                entries.append(f"  {name}/")
            else:
                sz = os.path.getsize(fp)
                entries.append(f"  {name}  ({sz} bytes)")
    except Exception as e:
        return f"Error: {e}"
    return "\n".join(entries) if entries else "(empty directory)"


# ws_grep and ws_glob executors have been moved to server/search/ package.
# They are registered via import in server/search/__init__.py.


@register_executor("ws_write_file", read_only=False, concurrent_safe=False)
def _exec_ws_write_file(tool_input, transcript, current_attachments):
    ws = get_workspace_path()
    if not ws:
        return _workspace_prompt_payload("ws_write_file", tool_input)
    session_id = tool_input.pop("__session_id__", "")
    path = tool_input.get("path", "")
    content = _normalize_lf(tool_input.get("content", ""))
    full = os.path.join(ws, path)
    if not _ws_path_allowed(full, ws, session_id):
        return outside_roots_error(path, ws, session_id)
    if not os.path.isfile(full):
        return f"File not found: {path}. Use ws_create_file for new files."
    # Write gate: must have a full prior read, file must not have changed
    allowed, reason = file_state.check_write_allowed(session_id, full)
    if not allowed:
        return f"Error: {reason}"
    try:
        with open(full, errors="replace") as f:
            original = f.read()
    except Exception as e:
        return f"Error reading original: {e}"
    payload = json.dumps(
        {
            "action": "write",
            "path": path,
            "content": content,
            "original": original,
            "session_id": session_id,
        }
    )
    return approval_request(payload)


# ── Quote normalization for ws_edit_file ──────────────────────────────
# Maps typographic/smart quotes to their ASCII equivalents for matching.
# When old_string contains typographic quotes, we normalize them for the
# search but preserve the original quoting style in the replacement.

_TYPOGRAPHIC_QUOTES = {
    "“": '"',  # left double quotation mark
    "”": '"',  # right double quotation mark
    "‘": "'",  # left single quotation mark
    "’": "'",  # right single quotation mark
    "«": '"',  # left-pointing double angle quotation mark
    "»": '"',  # right-pointing double angle quotation mark
    "‹": "'",  # single left-pointing angle quotation mark
    "›": "'",  # single right-pointing angle quotation mark
}


def _normalize_quotes(text: str) -> str:
    """Replace typographic/smart quotes with ASCII equivalents."""
    for typo, ascii_char in _TYPOGRAPHIC_QUOTES.items():
        text = text.replace(typo, ascii_char)
    return text


@register_executor("ws_edit_file", read_only=False, concurrent_safe=False)
def _exec_ws_edit_file(tool_input, transcript, current_attachments):
    """Edit a file by exact string replacement with quote normalization."""
    ws = get_workspace_path()
    if not ws:
        return _workspace_prompt_payload("ws_edit_file", tool_input)
    session_id = tool_input.pop("__session_id__", "")
    path = tool_input.get("path", "")
    old_string = tool_input.get("old_string", "")
    new_string = tool_input.get("new_string", "")
    replace_all = tool_input.get("replace_all", False)

    if not old_string:
        return "Error: old_string is required."
    if old_string == new_string:
        return "Error: old_string and new_string are identical."

    full = os.path.join(ws, path)
    if not _ws_path_allowed(full, ws, session_id):
        return outside_roots_error(path, ws, session_id)
    if not os.path.isfile(full):
        return f"File not found: {path}"

    # Write gate: must have a full prior read
    allowed, reason = file_state.check_write_allowed(session_id, full)
    if not allowed:
        return f"Error: {reason}"

    try:
        with open(full, errors="replace") as f:
            original = f.read()
    except Exception as e:
        return f"Error reading file: {e}"

    # Try exact match first
    count = original.count(old_string)

    # If no exact match, try with quote normalization
    used_normalization = False
    if count == 0:
        normalized_original = _normalize_quotes(original)
        normalized_old = _normalize_quotes(old_string)
        count = normalized_original.count(normalized_old)
        if count > 0:
            used_normalization = True
        else:
            return (
                f"Error: old_string not found in {path}. "
                "Ensure the text matches exactly, including whitespace and indentation."
            )

    if not replace_all and count > 1:
        return (
            f"Error: old_string found {count} times in {path}. "
            "Provide more surrounding context to make the match unique, "
            "or set replace_all=true to replace all occurrences."
        )

    # Compute the new content
    if used_normalization:
        # When using normalization, we need to find and replace using
        # the normalized version but apply changes to the original
        new_content = _replace_with_normalization(original, old_string, new_string, replace_all)
    else:
        if replace_all:
            new_content = original.replace(old_string, new_string)
        else:
            new_content = original.replace(old_string, new_string, 1)

    new_content = _normalize_lf(new_content)

    payload_data = {
        "action": "write",
        "path": path,
        "content": new_content,
        "original": original,
        "session_id": session_id,
    }
    # Flag a fuzzy match so the approval banner can tell the user the edit
    # only matched after quote normalization — the model's old_string did not
    # match the file byte-for-byte (typographic vs. ASCII quotes). Omitted on
    # exact matches so the hint only shows when it is actually relevant.
    if used_normalization:
        payload_data["matched_via_normalization"] = True
    payload = json.dumps(payload_data)
    return approval_request(payload)


def _replace_with_normalization(
    original: str, old_string: str, new_string: str, replace_all: bool
) -> str:
    """Replace text in original using quote-normalized matching.

    Finds positions where the normalized old_string matches in the
    normalized original, then replaces those spans in the actual original.
    """
    normalized_original = _normalize_quotes(original)
    normalized_old = _normalize_quotes(old_string)

    # Find all match positions in the normalized text
    positions = []
    start = 0
    while True:
        idx = normalized_original.find(normalized_old, start)
        if idx == -1:
            break
        positions.append(idx)
        if not replace_all:
            break
        start = idx + len(normalized_old)

    if not positions:
        return original

    # Build result by replacing spans in the original text
    # Since normalization is char-for-char (same length), positions map directly
    result = []
    prev_end = 0
    for pos in positions:
        result.append(original[prev_end:pos])
        result.append(new_string)
        prev_end = pos + len(normalized_old)
    result.append(original[prev_end:])
    return "".join(result)


def _stage_and_request_save(dest: str, filename: str, data: bytes) -> str:
    """Shared tail of every direct-to-destination save: validate the
    destination against the sensitive-path denylist, write the FINISHED
    bytes to the app-sandbox staging area, and return the [WS_APPROVAL]
    payload whose approval simply MOVES the ready file into place
    (server/approval/bootstrap.py's _do_save_to_path). By the time the user
    sees the card the file already exists — approving cannot fail on
    content problems, and declining leaves nothing outside the sandbox."""
    from server.security.sensitive_paths import is_sensitive_path

    if is_sensitive_path(dest):
        return f"Error: refusing to save to a sensitive path: {dest}"
    staged = stage_file_bytes(data, filename)
    payload = json.dumps(
        {
            "action": "save_to_path",
            "path": dest,
            "filename": filename,
            "staged_path": staged,
            "size": len(data),
        }
    )
    return approval_request(payload)


@register_executor("ws_create_file", read_only=False, concurrent_safe=False)
def _exec_ws_create_file(tool_input, transcript, current_attachments):
    resolved = resolve_write_destination("ws_create_file", tool_input)
    if isinstance(resolved, str):
        return resolved
    kind, target = resolved
    content = tool_input.get("content", "")

    if kind == "absolute":
        # No workspace connected (or the user named an explicit location):
        # stage the finished content and request the move — never connects
        # a persistent workspace.
        return _stage_and_request_save(target, os.path.basename(target), content.encode("utf-8"))

    full = target
    path = tool_input.get("path", "")
    if os.path.isdir(full):
        return f"Directory already exists: {path}. You don't need to create directories — they are created automatically when you create files inside them. Proceed to create the files directly."
    if os.path.isfile(full):
        return f"File already exists: {path}. Use ws_write_file to modify it."
    payload = json.dumps(
        {
            "action": "create",
            "path": path,
            "content": content,
            "session_id": tool_input.get("__session_id__", ""),
        }
    )
    return approval_request(payload)


@register_executor("save_file", read_only=False, concurrent_safe=False)
def _exec_save_file(tool_input, transcript, current_attachments):
    """One-shot 'save this file somewhere' — never connects a persistent
    workspace (get_workspace_path() is not even consulted here) and never
    asks the user where to save.

    Destination comes from destination_path when the model resolved one
    from the user's own words; otherwise it defaults to the user's
    Documents or Downloads folder (suggested_location, default Documents).
    Content is decoded and staged immediately; the approval card then just
    moves the ready file into place.
    """
    filename = os.path.basename((tool_input.get("filename") or "").strip())
    if not filename:
        return "Error: filename is required."

    destination_path = tool_input.get("destination_path")
    if destination_path:
        raw_dest = os.path.expanduser(str(destination_path))
        if not os.path.isabs(raw_dest):
            return f"Error: destination_path must be an absolute path (got {destination_path!r})."
        dest = os.path.realpath(raw_dest)
    else:
        folder = tool_input.get("suggested_location") or "Documents"
        if folder not in ("Documents", "Downloads"):
            folder = "Documents"
        dest = os.path.join(os.path.expanduser(f"~/{folder}"), filename)

    content = tool_input.get("content", "")
    encoding = tool_input.get("content_encoding") or "utf-8"
    if encoding not in ("utf-8", "base64"):
        return f"Error: content_encoding must be 'utf-8' or 'base64' (got {encoding!r})."
    if encoding == "base64":
        try:
            data = base64.b64decode(content, validate=True)
        except Exception as e:
            return f"Error: content is not valid base64: {e}"
    else:
        data = content.encode("utf-8")

    return _stage_and_request_save(dest, filename, data)


@register_executor("ws_delete_file", read_only=False, concurrent_safe=False, destructive=True)
def _exec_ws_delete_file(tool_input, transcript, current_attachments):
    ws = get_workspace_path()
    if not ws:
        return _workspace_prompt_payload("ws_delete_file", tool_input)
    session_id = tool_input.pop("__session_id__", "")
    path = tool_input.get("path", "")
    full = os.path.join(ws, path)
    if not _ws_path_allowed(full, ws, session_id):
        return outside_roots_error(path, ws, session_id)
    if not os.path.exists(full):
        return f"File not found: {path}"
    try:
        with open(full, errors="replace") as f:
            original = f.read()
    except Exception:
        original = "(binary or unreadable)"
    payload = json.dumps(
        {"action": "delete", "path": path, "original": original, "session_id": session_id}
    )
    return approval_request(payload)


@register_executor("ws_run_command", read_only=False, concurrent_safe=False)
def _exec_ws_run_command(tool_input, transcript, current_attachments):
    ws = get_workspace_path()
    if not ws:
        return "No workspace connected."
    command = tool_input.get("command", "").strip()
    if not command:
        return "No command provided."
    session_id = tool_input.pop("__session_id__", "")
    run_in_background = bool(tool_input.get("run_in_background", False))
    # Bypass reads the credential stores with no card; anywhere else a
    # request for them goes to the approval gate (server/security/secret_reads.py).
    bypass = secret_reads_policy.stamped(tool_input)
    asked = secret_reads_policy.requested(tool_input)

    # Read-only commands execute directly, no approval needed. The read-only
    # sandbox refuses any write the classifier missed.
    if _is_read_only_command(command) and (bypass or not asked):
        warning = _validate_command(command, secret_reads=bypass)
        if warning:
            return f"Error: {warning}"
        try:
            return run_workspace_command(
                command,
                ws,
                session_id=session_id,
                run_in_background=run_in_background,
                write_mode="readonly",
                secret_reads=bypass,
            ).text
        except Exception as e:
            return f"Error: {e}"

    # Everything else runs through the same path once approved: the session
    # keeps its cwd and owns the task, and run_in_background plus the 30s
    # handoff apply exactly as they do to read-only commands. The payload names
    # the folder the command will run in (the session's tracked cwd, which an
    # earlier `cd` may have moved), so the card shows it and the rm floor
    # resolves a relative delete where it lands.
    from server.cwd_tracker import get_cwd

    request = {
        "action": "command",
        "command": command,
        "cwd": get_cwd(session_id, ws) if session_id else ws,
        "session_id": session_id,
        "run_in_background": run_in_background,
    }
    if asked:
        request[secret_reads_policy.SECRET_ACCESS_KEY] = True
    return approval_request(json.dumps(request))


_AUTO_BACKGROUND_SECONDS = 30


@dataclass
class CommandRun:
    """What one ws_run_command execution produced for the model."""

    text: str
    background: bool = False
    returncode: int | None = None
    # A session Stop killed the command before it finished.
    stopped: bool = False


def run_workspace_command(
    command: str,
    ws: str,
    *,
    session_id: str = "",
    run_in_background: bool = False,
    write_mode: str = "open",
    secret_reads: bool = False,
) -> CommandRun:
    """The one execution path for ws_run_command, read-only and approved alike.

    The caller has already validated ``command``. Starts it at once as a
    background task when asked to; otherwise waits up to
    _AUTO_BACKGROUND_SECONDS and, if it is still running then, hands the SAME
    process to a background task (no restart, no kill) and returns its handle
    so the model polls instead of blocking. Blocking: call it off the event
    loop. ``write_mode`` is the sandbox's: "readonly" for a command that skipped
    approval, "workspace" for one a mode, a session answer, a rule, the
    classifier or an agent let through, "open" for one a person approved on
    its card (server.approval.executors._do_command). A confined run is
    confined to the workspace itself, not to the tracked cwd, so a session
    that cd'd out of the workspace still cannot write there; the session's own
    scratch folder is writable beside it, and holds its package caches.
    ``secret_reads`` lets it read the credential stores (server/security/
    secret_reads.py); without it, a failure that looks like the sandbox hid
    one says how to ask.
    """
    from server.cwd_tracker import (
        extract_cwd_from_output,
        generation,
        get_cwd,
        update_cwd,
        wrap_command_for_cwd,
    )
    from server.tasks.handoff import run_with_handoff

    # Captured before the command starts, so its cwd write-back is refused if
    # the workspace is disconnected or switched while it runs.
    cwd_generation = generation()
    effective_cwd = get_cwd(session_id, ws) if session_id else ws
    scratch_dir = own_scratch_dir(session_id)
    redirected = _apply_stdin_redirect(command) if _needs_stdin_redirect(command) else command
    exec_command = wrap_command_for_cwd(redirected)

    if run_in_background:
        text = _start_background_command(
            command,
            exec_command,
            effective_cwd,
            session_id,
            write_mode,
            write_root=ws,
            scratch_dir=scratch_dir,
            secret_reads=secret_reads,
        )
        return CommandRun(text=text, background=True)

    result = run_with_handoff(
        command,
        exec_command,
        cwd=effective_cwd,
        session_id=session_id,
        timeout=_AUTO_BACKGROUND_SECONDS,
        write_mode=write_mode,
        write_root=ws,
        scratch_dir=scratch_dir,
        secret_reads=secret_reads,
    )
    if result.background:
        text = _background_started_message(command, result.task_id, result.output_path)
        return CommandRun(text=text, background=True)
    clean_output, new_cwd = extract_cwd_from_output(result.output.strip())
    if session_id and new_cwd and os.path.isdir(new_cwd):
        update_cwd(session_id, new_cwd, generation=cwd_generation)
    if clean_output:
        output = clean_output
    elif result.returncode == 0 and _is_silent_command(command):
        output = "Done."
    else:
        output = "(no output)"
    output = _truncate_shell_output(output)
    meaning = _interpret_exit_code(command, result.returncode)
    if meaning:
        output += f"\n(exit code {result.returncode}: {meaning})"
    elif result.returncode not in (0, None):
        # Said for every failure, so neither the model nor the claim check
        # (server/goals/evidence.py) reads a failed command as done.
        output += f"\n(exit code {result.returncode})"
    if write_mode != "open" and result.returncode not in (0, None):
        # A confined run that failed on a write the OS refused: say which
        # sandbox it was and how to ask for more, the way terminal_run does.
        from server.sandbox import classify_sandbox_denial

        hint = classify_sandbox_denial(output, write_mode)
        if hint:
            output += "\n" + hint
    if not secret_reads and result.returncode not in (0, None):
        hint = secret_reads_policy.denial_hint(command, output)
        if hint:
            output += "\n" + hint
    if result.task_id:
        output += "\n" + _left_running_message(result.task_id, result.output_path)
    return CommandRun(text=output, returncode=result.returncode, stopped=result.stopped)


def _left_running_message(task_id: str, output_path: str | None) -> str:
    return (
        f"[Left running] The command returned, but processes it started in the "
        f"background are still running, tracked as task_id={task_id}. Their output "
        f"keeps going to {output_path}; check them with task_status or task_output "
        f"{{'task_id': '{task_id}'}} and stop them with task_cancel."
    )


def _start_background_command(
    command: str,
    exec_command: str,
    cwd: str,
    session_id: str = "",
    write_mode: str = "open",
    *,
    write_root: str | None = None,
    scratch_dir: str | None = None,
    secret_reads: bool = False,
) -> str:
    """Start a command in the background and return a status message."""
    from server.tasks.shell import start_shell_task

    task_info = start_shell_task(
        command,
        cwd=cwd,
        session_id=session_id,
        exec_command=exec_command,
        write_mode=write_mode,
        write_root=write_root,
        scratch_dir=scratch_dir,
        secret_reads=secret_reads,
    )
    return _background_started_message(command, task_info["task_id"], task_info["output_path"])


def _background_started_message(command: str, task_id: str, output_path: str) -> str:
    return (
        f"[Background Task Started] task_id={task_id}\n"
        f"Command: {command[:200]}\n"
        f"The command is running in the background; you will see a task event "
        f"in this session when it finishes. Check on it with task_status "
        f"{{'task_id': '{task_id}'}} and read its output with task_output "
        f"{{'task_id': '{task_id}'}} — wait a while between checks (30s, then "
        f"1-2 min) rather than polling immediately. Raw output file: {output_path}"
    )


# =============================================================================
# Feature 8: Git Worktree Isolation
# =============================================================================

_WORKTREES: dict[str, dict] = {}  # name -> {path, branch, base_branch}


@register_executor("ws_create_worktree", read_only=False, concurrent_safe=False)
def _exec_ws_create_worktree(tool_input, transcript, current_attachments):
    ws = get_workspace_path()
    if not ws:
        return "No workspace connected."
    git_dir = os.path.join(ws, ".git")
    if not os.path.isdir(git_dir):
        return "Workspace is not a git repository."
    name = tool_input.get("name", "").strip()
    if not name:
        import time

        name = f"worktree-{int(time.time())}"
    # Validate the name before it becomes a filesystem path segment and a git
    # branch component. Without this, values like "../evil" or "a;b" flow into
    # os.path.join(ws, '.worktrees', name) and f'whisper/{name}' (path traversal
    # / branch-name injection). Reuse the same guard enter_worktree uses.
    from server.git.worktree_session import validate_worktree_slug

    try:
        validate_worktree_slug(name)
    except ValueError as e:
        return f"Invalid worktree name: {e}"
    branch = f"whisper/{name}"
    worktree_path = os.path.join(ws, ".worktrees", name)
    try:
        result = subprocess.run(
            ["git", "worktree", "add", "-b", branch, worktree_path],
            capture_output=True,
            text=True,
            cwd=ws,
            timeout=15,
        )
        if result.returncode != 0:
            return f"Failed to create worktree: {result.stderr.strip()}"
        _WORKTREES[name] = {"path": worktree_path, "branch": branch, "base": "HEAD"}
        return json.dumps({"created": True, "name": name, "branch": branch, "path": worktree_path})
    except Exception as e:
        return f"Worktree error: {e}"


@register_executor("ws_diff_worktree", read_only=True, concurrent_safe=True)
def _exec_ws_diff_worktree(tool_input, transcript, current_attachments):
    ws = get_workspace_path()
    if not ws:
        return "No workspace connected."
    name = tool_input.get("name", "")
    info = _WORKTREES.get(name)
    if not info:
        return f"Worktree '{name}' not found."
    try:
        result = subprocess.run(
            ["git", "diff", f"HEAD...{info['branch']}"],
            capture_output=True,
            text=True,
            cwd=ws,
            timeout=15,
        )
        diff = result.stdout.strip()
        return diff if diff else "(no changes in worktree)"
    except Exception as e:
        return f"Diff error: {e}"


@register_executor("ws_merge_worktree", read_only=False, concurrent_safe=False)
def _exec_ws_merge_worktree(tool_input, transcript, current_attachments):
    ws = get_workspace_path()
    if not ws:
        return "No workspace connected."
    name = tool_input.get("name", "")
    info = _WORKTREES.get(name)
    if not info:
        return f"Worktree '{name}' not found."
    # Run the merge with ARGV, never a shell string. Git ref names permit
    # shell-active characters (brace expansion `{a,b}`, `;`, `(`) and quotes,
    # so interpolating the branch/name into a shell `command` payload was a
    # command-injection vector. Passing them as literal argv elements — the
    # same way do_git_merge and the sibling worktree ops (ws_create_worktree,
    # ws_diff_worktree) do — means the shell never parses them. The `-m`
    # message keeps the user-facing behavior (a descriptive merge commit).
    branch = info["branch"]
    msg = f"Merge worktree {name}"
    try:
        result = subprocess.run(
            ["git", "merge", "--no-ff", branch, "-m", msg],
            capture_output=True,
            text=True,
            cwd=ws,
            timeout=30,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            return f"Failed to merge worktree: {detail}"
        output = (result.stdout + result.stderr).strip()
        return json.dumps({"merged": True, "name": name, "branch": branch, "output": output})
    except Exception as e:
        return f"Merge error: {e}"
