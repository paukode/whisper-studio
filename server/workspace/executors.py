"""All `@register_executor` handlers for workspace tools.

Imports from sibling modules (paths/state/filesystem/commands) — no FastAPI
or route deps. The decorators fire at import time, so the package `__init__.py`
must import this module for tool registration to take effect.

Also home to:
- execute_ws_open_folder: the public bridge used by chat.py / tool_router.py
- _normalize_quotes + _replace_with_normalization: quote-tolerant matching
  used by ws_edit_file
- _is_read_only_command: classifier that decides whether ws_run_command
  bypasses the approval dialog
- _WORKTREES: in-memory registry shared with the worktree route handlers
"""

import json
import logging
import os
import re
import subprocess

from server import file_state
from server.executors import register_executor

from .commands import (
    _apply_stdin_redirect,
    _interpret_exit_code,
    _needs_stdin_redirect,
    _truncate_shell_output,
    _validate_command,
)
from .filesystem import _ws_list_dir
from .paths import (
    _WS_BINARY_EXTS,
    WORKSPACE_BACKUPS,
    _normalize_lf,
    _ws_validate_path,
)
from .state import (
    _workspace_prompt_payload,
    get_workspace_path,
    load_workspace_config,
    save_recent_workspace,
    save_workspace_config,
)

log = logging.getLogger("whisper-studio")


def execute_ws_open_folder(tool_input: dict) -> str:
    """Create folder if needed, connect workspace, return connection info."""
    path = tool_input.get("path", "").strip()
    if not path:
        return json.dumps({"error": "path is required"})
    path = os.path.expanduser(path)
    path = os.path.realpath(path)
    existing = get_workspace_path()
    if existing and os.path.realpath(existing) != path:
        return json.dumps(
            {
                "error": f"A workspace is already connected at '{existing}'. Use ws_read_file, ws_write_file, and ws_create_file to work with it. Do not switch workspaces unless the user explicitly asks."
            }
        )
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as e:
        return json.dumps({"error": f"Could not create folder: {e}"})
    # Connect workspace
    config = load_workspace_config()
    config["path"] = path
    config["mode"] = "chat"
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


# --- Workspace Tool Executors ---


@register_executor("ws_read_file", read_only=True, concurrent_safe=True)
def _exec_ws_read_file(tool_input, transcript, current_attachments):
    ws = get_workspace_path()
    if not ws:
        return "No workspace connected."
    session_id = tool_input.pop("__session_id__", "")
    path = tool_input.get("path", "")
    full = os.path.join(ws, path)
    if not _ws_validate_path(full, ws):
        return "Error: path outside workspace."
    if not os.path.isfile(full):
        return f"File not found: {path}"
    ext = os.path.splitext(path)[1].lower()
    if ext in _WS_BINARY_EXTS:
        return f"Binary file: {path} ({os.path.getsize(full)} bytes)"
    offset = tool_input.get("offset", 1)
    limit = tool_input.get("limit")
    # Dedup: return stub if same file read with same params and unchanged
    stub = file_state.check_dedup(session_id, path, full, offset, limit)
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

    file_state.record_read(session_id, path, full, offset, limit, len(numbered))
    return result


@register_executor("ws_list_directory", read_only=True, concurrent_safe=True)
def _exec_ws_list_dir(tool_input, transcript, current_attachments):
    ws = get_workspace_path()
    if not ws:
        return "No workspace connected."
    path = tool_input.get("path", "").strip() or "."
    full = os.path.join(ws, path) if path != "." else ws
    if not _ws_validate_path(full, ws):
        return "Error: path outside workspace."
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
        return _workspace_prompt_payload("ws_write_file", tool_input, "no_workspace")
    session_id = tool_input.pop("__session_id__", "")
    path = tool_input.get("path", "")
    content = _normalize_lf(tool_input.get("content", ""))
    full = os.path.join(ws, path)
    if not _ws_validate_path(full, ws):
        return _workspace_prompt_payload("ws_write_file", tool_input, "outside_workspace")
    if not os.path.isfile(full):
        return f"File not found: {path}. Use ws_create_file for new files."
    # Write gate: must have a full prior read, file must not have changed
    allowed, reason = file_state.check_write_allowed(session_id, path, full)
    if not allowed:
        return f"Error: {reason}"
    try:
        with open(full, errors="replace") as f:
            original = f.read()
    except Exception as e:
        return f"Error reading original: {e}"
    payload = json.dumps(
        {"action": "write", "path": path, "content": content, "original": original}
    )
    return f"[WS_APPROVAL]{payload}"


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
        return _workspace_prompt_payload("ws_edit_file", tool_input, "no_workspace")
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
    if not _ws_validate_path(full, ws):
        return _workspace_prompt_payload("ws_edit_file", tool_input, "outside_workspace")
    if not os.path.isfile(full):
        return f"File not found: {path}"

    # Write gate: must have a full prior read
    allowed, reason = file_state.check_write_allowed(session_id, path, full)
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
    }
    # Flag a fuzzy match so the approval banner can tell the user the edit
    # only matched after quote normalization — the model's old_string did not
    # match the file byte-for-byte (typographic vs. ASCII quotes). Omitted on
    # exact matches so the hint only shows when it is actually relevant.
    if used_normalization:
        payload_data["matched_via_normalization"] = True
    payload = json.dumps(payload_data)
    return f"[WS_APPROVAL]{payload}"


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


@register_executor("ws_create_file", read_only=False, concurrent_safe=False)
def _exec_ws_create_file(tool_input, transcript, current_attachments):
    ws = get_workspace_path()
    if not ws:
        return _workspace_prompt_payload("ws_create_file", tool_input, "no_workspace")
    path = tool_input.get("path", "")
    content = tool_input.get("content", "")
    full = os.path.join(ws, path)
    if not _ws_validate_path(full, ws):
        return _workspace_prompt_payload("ws_create_file", tool_input, "outside_workspace")
    if os.path.isdir(full):
        return f"Directory already exists: {path}. You don't need to create directories — they are created automatically when you create files inside them. Proceed to create the files directly."
    if os.path.isfile(full):
        return f"File already exists: {path}. Use ws_write_file to modify it."
    payload = json.dumps({"action": "create", "path": path, "content": content})
    return f"[WS_APPROVAL]{payload}"


@register_executor("ws_delete_file", read_only=False, concurrent_safe=False, destructive=True)
def _exec_ws_delete_file(tool_input, transcript, current_attachments):
    ws = get_workspace_path()
    if not ws:
        return _workspace_prompt_payload("ws_delete_file", tool_input, "no_workspace")
    path = tool_input.get("path", "")
    full = os.path.join(ws, path)
    if not _ws_validate_path(full, ws):
        return _workspace_prompt_payload("ws_delete_file", tool_input, "outside_workspace")
    if not os.path.exists(full):
        return f"File not found: {path}"
    try:
        with open(full, errors="replace") as f:
            original = f.read()
    except Exception:
        original = "(binary or unreadable)"
    payload = json.dumps({"action": "delete", "path": path, "original": original})
    return f"[WS_APPROVAL]{payload}"


_READ_ONLY_COMMAND_PREFIXES = frozenset(
    {
        "git status",
        "git diff",
        "git log",
        "git show",
        "git branch",
        "git tag",
        "git remote",
        "git stash list",
        "git rev-parse",
        "git describe",
        "git shortlog",
        "git blame",
        "git ls-files",
        "git ls-tree",
        "ls",
        "cat",
        "head",
        "tail",
        "wc",
        "file",
        "stat",
        "du",
        "df",
        "find",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
        "echo",
        "printf",
        "date",
        "whoami",
        "hostname",
        "uname",
        "pwd",
        "which",
        "where",
        "type",
        "env",
        "printenv",
        "diff",
        "cmp",
        "sort",
        "uniq",
        "tr",
        "cut",
        "sed -n",
        "tree",
        "readlink",
        "realpath",
        "basename",
        "dirname",
    }
)

# `find` action predicates that write or execute — their presence turns an
# otherwise read-only `find` into a mutation/arbitrary-exec path.
_FIND_WRITE_ACTIONS = frozenset(
    {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls"}
)


def _segment_is_read_only(seg: str) -> bool:
    """True only if a single (unpiped) command segment is provably read-only.

    Fails closed: rejects output redirection to a real file and, for ``find``,
    any action predicate that writes or executes. Interpreters like
    ``python -c`` / ``node -e`` / ``awk`` are deliberately NOT in the allowlist
    because they run arbitrary code (``python3 -c "shutil.rmtree(...)"``).
    """
    seg = seg.strip()
    if not seg:
        return False
    # Strip quoted content so a quoted '>' or metacharacter does not count,
    # then allow only harmless stderr merges / /dev/null sinks; any remaining
    # '>' is a real file write and forces approval.
    unquoted = re.sub(r"'[^']*'|\"[^\"]*\"", "", seg)
    safe_redirs = re.sub(r"\d*>&\d+|\d*>>?\s*/dev/null", "", unquoted)
    if ">" in safe_redirs:
        return False
    if seg == "find" or seg.startswith("find ") or seg.startswith("find\t"):
        if any(tok in _FIND_WRITE_ACTIONS for tok in unquoted.split()):
            return False
    for prefix in _READ_ONLY_COMMAND_PREFIXES:
        if seg == prefix or seg.startswith(prefix + " ") or seg.startswith(prefix + "\t"):
            return True
    return False


def _is_read_only_command(command: str) -> bool:
    """Check if a shell command is read-only (safe to execute without approval).

    A command is read-only only if EVERY pipe segment is independently
    read-only, so a read-only head piped into an interpreter
    (``cat script | bash``, ``echo payload | sh``) does NOT bypass approval.
    """
    # Strip leading cd ... && or cd ... ;
    cmd = command.strip()
    # Handle "cd /path && actual_command" pattern
    if cmd.startswith("cd "):
        for sep in (" && ", "; "):
            idx = cmd.find(sep)
            if idx != -1:
                cmd = cmd[idx + len(sep) :].strip()
                break
    # Every pipe segment must be read-only (empty segments, e.g. from `||`,
    # fail closed and force approval).
    return all(_segment_is_read_only(seg) for seg in cmd.split("|"))


@register_executor("ws_run_command", read_only=False, concurrent_safe=False)
def _exec_ws_run_command(tool_input, transcript, current_attachments):
    ws = get_workspace_path()
    if not ws:
        return "No workspace connected."
    command = tool_input.get("command", "").strip()
    if not command:
        return "No command provided."

    # Read-only commands execute directly — no approval needed
    if _is_read_only_command(command):
        return _execute_command_directly(command, ws, tool_input)

    payload = json.dumps({"action": "command", "command": command, "cwd": ws})
    return f"[WS_APPROVAL]{payload}"


_AUTO_BACKGROUND_SECONDS = 30


def _execute_command_directly(command: str, ws: str, tool_input: dict) -> str:
    """Execute a read-only command directly and return output to the LLM.

    If the command runs longer than _AUTO_BACKGROUND_SECONDS, it is
    automatically moved to a background task and a task_id is returned
    so the LLM can poll for results instead of blocking.
    """
    warning = _validate_command(command)
    if warning:
        return f"Error: {warning}"

    run_in_background = tool_input.get("run_in_background", False)
    session_id = tool_input.pop("__session_id__", "")
    from server.cwd_tracker import (
        extract_cwd_from_output,
        get_cwd,
        update_cwd,
        wrap_command_for_cwd,
    )

    effective_cwd = get_cwd(session_id, ws) if session_id else ws
    redirected = _apply_stdin_redirect(command) if _needs_stdin_redirect(command) else command
    exec_command = wrap_command_for_cwd(redirected)

    # Explicit background request — start immediately in background
    if run_in_background:
        return _start_background_command(command, exec_command, effective_cwd, session_id)

    try:
        from server.tasks.handoff import run_with_handoff

        result = run_with_handoff(
            command,
            exec_command,
            cwd=effective_cwd,
            session_id=session_id,
            timeout=_AUTO_BACKGROUND_SECONDS,
        )
        if result.background:
            # Command outlived the foreground budget: the SAME process keeps
            # running as a background task (no restart, no lost work).
            return _background_started_message(command, result.task_id, result.output_path)
        output_text = result.output.strip()
        clean_output, new_cwd = extract_cwd_from_output(output_text)
        if session_id and new_cwd and os.path.isdir(new_cwd):
            update_cwd(session_id, new_cwd)
        if not clean_output:
            output = "(no output)"
        else:
            output = clean_output
        output = _truncate_shell_output(output)
        meaning = _interpret_exit_code(command, result.returncode)
        if meaning:
            output += f"\n(exit code {result.returncode}: {meaning})"
        return output
    except Exception as e:
        return f"Error: {e}"


def _start_background_command(
    command: str, exec_command: str, cwd: str, session_id: str = ""
) -> str:
    """Start a command in the background and return a status message."""
    from server.tasks.shell import start_shell_task

    task_info = start_shell_task(command, cwd=cwd, session_id=session_id, exec_command=exec_command)
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
