"""
Git tool executor — handles git tool calls with safety validation.

Each tool handler validates inputs against the security layer, executes
git commands via subprocess.run(), parses output, tracks operations,
and returns formatted results for the AI assistant.
"""

import os
import subprocess

from server.executors import register_executor
from server.git.core import (
    _run_git,
    get_branch,
    get_default_branch,
)
from server.git.diff import fetch_single_file_git_diff
from server.git.filesystem import is_safe_ref_name
from server.git.security import contains_secret_files
from server.git.tracking import track_git_operations
from server.workspace import get_workspace_path

# --- Helpers ---


def _get_git_cwd():
    """Get workspace path and validate it's a git repo. Returns (cwd, error_msg)."""
    ws = get_workspace_path()
    if not ws:
        return None, "No workspace connected. Connect a workspace first."
    git_path = os.path.join(ws, ".git")
    if not os.path.exists(git_path):
        return None, "Workspace is not a git repository."
    return ws, None


def _git(args, cwd, timeout=15):
    """Run git command, return (stdout, stderr, returncode)."""
    result = _run_git(args, cwd=cwd, timeout=timeout)
    return result.stdout, result.stderr, result.returncode


def _truncate(text, max_lines=200, max_chars=50000):
    """Truncate output for tool results."""
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n... (truncated at {max_chars} chars)"
    lines = text.split("\n")
    if len(lines) > max_lines:
        text = "\n".join(lines[:max_lines]) + f"\n\n... ({len(lines) - max_lines} more lines)"
    return text


def _coerce_limit(value, default=20, lo=1, hi=1000):
    """Coerce a model-supplied commit limit to a bounded int.

    Guards `git log -{limit}`: a non-int value (or one carrying option-like
    text such as "-output=/path") falls back to the default, and the result
    is clamped to [lo, hi] so nothing but a plain integer reaches the argv.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


# --- Read tool executors ---


@register_executor("git_status", read_only=True, concurrent_safe=True)
def _exec_git_status(tool_input, transcript, current_attachments):
    cwd, err = _get_git_cwd()
    if err:
        return err
    tool_input.pop("__session_id__", "")
    short = tool_input.get("short", False)
    args = ["status"]
    if short:
        args.append("--short")
    stdout, stderr, rc = _git(args, cwd)
    if rc != 0:
        return f"Error: {stderr.strip()}"
    return stdout.strip() or "(clean working tree)"


@register_executor("git_diff", read_only=True, concurrent_safe=True)
def _exec_git_diff(tool_input, transcript, current_attachments):
    cwd, err = _get_git_cwd()
    if err:
        return err
    tool_input.pop("__session_id__", "")
    cached = tool_input.get("cached", False)
    stat_only = tool_input.get("stat", False)
    single_file = tool_input.get("file")

    # Single file diff (PR-like, against merge base)
    if single_file:
        abs_file = os.path.join(cwd, single_file)
        result = fetch_single_file_git_diff(cwd, abs_file)
        if not result:
            return f"No diff for {single_file} (file may be unchanged or not found)."
        header = f"File: {result.filename} ({result.status})\n"
        header += f"+{result.additions} -{result.deletions} ({result.changes} changes)\n\n"
        return _truncate(header + result.patch)

    args = ["--no-optional-locks", "diff"]
    if cached:
        args.append("--cached")
    else:
        args.append("HEAD")
    if stat_only:
        args.append("--stat")

    stdout, stderr, rc = _git(args, cwd)
    if rc != 0:
        return f"Error: {stderr.strip()}"
    return _truncate(stdout.strip()) or "(no changes)"


@register_executor("git_log", read_only=True, concurrent_safe=True)
def _exec_git_log(tool_input, transcript, current_attachments):
    cwd, err = _get_git_cwd()
    if err:
        return err
    tool_input.pop("__session_id__", "")
    limit = _coerce_limit(tool_input.get("limit", 20))
    branch = tool_input.get("branch")
    file_path = tool_input.get("file")
    oneline = tool_input.get("oneline", True)

    # `branch` sits before the `--` separator, so it must be validated —
    # an option-like value (e.g. "--output=/path") would otherwise be
    # parsed by git as a flag rather than a revision.
    if branch and not is_safe_ref_name(branch):
        return f"Error: invalid ref name: {branch}"

    args = ["log", f"-{limit}"]
    if oneline:
        args.append("--oneline")
    else:
        args.extend(["--format=%h %an %ad %s", "--date=relative"])
    if branch:
        args.append(branch)
    if file_path:
        args.extend(["--", file_path])

    stdout, stderr, rc = _git(args, cwd)
    if rc != 0:
        return f"Error: {stderr.strip()}"
    return stdout.strip() or "(no commits)"


@register_executor("git_branch_list", read_only=True, concurrent_safe=True)
def _exec_git_branch_list(tool_input, transcript, current_attachments):
    cwd, err = _get_git_cwd()
    if err:
        return err
    tool_input.pop("__session_id__", "")
    show_all = tool_input.get("all", False)
    remote_only = tool_input.get("remote", False)

    args = ["branch"]
    if show_all:
        args.append("-a")
    elif remote_only:
        args.append("-r")

    stdout, stderr, rc = _git(args, cwd)
    if rc != 0:
        return f"Error: {stderr.strip()}"
    return stdout.strip() or "(no branches)"


@register_executor("git_show", read_only=True, concurrent_safe=True)
def _exec_git_show(tool_input, transcript, current_attachments):
    cwd, err = _get_git_cwd()
    if err:
        return err
    tool_input.pop("__session_id__", "")
    ref = tool_input.get("ref", "HEAD")
    stat_only = tool_input.get("stat", False)

    # The ref is a positional revision that precedes the `--` separator, so
    # the separator alone cannot stop an option-like value ("--output=/path")
    # from being parsed as a git flag — validate it against the ref allowlist.
    if ref and not is_safe_ref_name(ref):
        return f"Error: invalid ref name: {ref}"

    args = ["show"]
    if stat_only:
        args.append("--stat")
    # Trailing `--` marks the end of options / start of pathspecs so nothing
    # that follows can be reinterpreted as a flag.
    args.extend([ref, "--"])

    stdout, stderr, rc = _git(args, cwd)
    if rc != 0:
        return f"Error: {stderr.strip()}"
    return _truncate(stdout.strip())


@register_executor("git_blame", read_only=True, concurrent_safe=True)
def _exec_git_blame(tool_input, transcript, current_attachments):
    cwd, err = _get_git_cwd()
    if err:
        return err
    tool_input.pop("__session_id__", "")
    file_path = tool_input.get("file", "")
    line_start = tool_input.get("line_start")
    line_end = tool_input.get("line_end")

    if not file_path:
        return "Error: 'file' parameter is required."

    args = ["blame"]
    if line_start and line_end:
        args.extend(["-L", f"{line_start},{line_end}"])
    elif line_start:
        args.extend(["-L", f"{line_start},"])
    # `--` before the file makes it a pathspec, so an option-like value
    # ("--output=/path") is treated as a filename, never a git flag.
    args.extend(["--", file_path])

    stdout, stderr, rc = _git(args, cwd)
    if rc != 0:
        return f"Error: {stderr.strip()}"
    return _truncate(stdout.strip())


@register_executor("git_stash_list", read_only=True, concurrent_safe=True)
def _exec_git_stash_list(tool_input, transcript, current_attachments):
    cwd, err = _get_git_cwd()
    if err:
        return err
    tool_input.pop("__session_id__", "")

    stdout, stderr, rc = _git(["stash", "list"], cwd)
    if rc != 0:
        return f"Error: {stderr.strip()}"
    return stdout.strip() or "(no stashes)"


# --- Write tool executors ---

# --- Approval-gated write helpers (called by ApprovalSpec executors) ---
#
# The tool registrations (below) return a `[WS_APPROVAL]` payload so the
# user sees a card and approves the action. These helpers run the real
# git command, invoked from server/approval/bootstrap.py once approval
# is granted (or session-approved with "Yes, all cli").


# Anchored regex for accepted git remote URL forms. Allows alnum, dot,
# slash, at, colon, underscore, hyphen — explicitly NO shell metachars,
# spaces, semicolons, backticks, or quotes. Rejects file:// to keep
# the assistant from cloning arbitrary local paths.
import re as _re  # noqa: E402

_GIT_URL_RE = _re.compile(r"^(https?://|ssh://|git@)[\w./@:_-]+$")


def _derive_clone_dest(url: str) -> str:
    """Default destination: ~/Documents/<repo-name>. Strips trailing .git."""
    base = url.rstrip("/").split("/")[-1]
    if base.endswith(".git"):
        base = base[:-4]
    if not base:
        base = "repo"
    return os.path.join(os.path.expanduser("~"), "Documents", base)


def do_git_clone(payload: dict) -> tuple[bool, str]:
    """Clone a remote repo, optionally connect as workspace. Returns
    (ok, output_or_error). Mirrors the (bool, str) shape every other
    approval-gated git helper uses so bootstrap.py can wrap it uniformly."""
    url = (payload.get("url") or "").strip()
    if not url:
        return False, "url is required"
    if not _GIT_URL_RE.match(url):
        return False, (
            f"invalid url {url!r} — accepted forms are https://, ssh://, "
            "or git@host:path (no shell metacharacters, no file://)"
        )

    dest_in = (payload.get("dest") or "").strip()
    dest = os.path.expanduser(dest_in) if dest_in else _derive_clone_dest(url)
    dest = os.path.realpath(dest) if os.path.exists(dest) else os.path.abspath(dest)

    if os.path.exists(dest) and os.path.isdir(dest) and os.listdir(dest):
        return False, f"destination already exists and is not empty: {dest}"
    if os.path.exists(dest) and not os.path.isdir(dest):
        return False, f"destination exists but is not a directory: {dest}"

    parent = os.path.dirname(dest)
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as e:
        return False, f"cannot create parent directory {parent}: {e}"

    # Subprocess hygiene matches _run_git: no shell, no stdin, no credential
    # prompt hang. The clone runs as a separate process (not via _run_git)
    # because git clone needs to be invoked from the parent directory
    # without a pre-existing repo.
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    from server.git.core import get_git_exe

    try:
        result = subprocess.run(
            [get_git_exe(), "clone", "--", url, dest],
            capture_output=True,
            text=True,
            cwd=parent,
            timeout=300,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, "git clone timed out after 300s — network too slow or repo too large"

    if result.returncode != 0:
        return False, f"git clone failed: {(result.stderr or result.stdout or '').strip()}"

    # Default `open` to True. Auto-connect routes through the same helper
    # the REST endpoint uses so config / watcher / recents all stay in sync.
    should_open = payload.get("open", True)
    suffix = ""
    if should_open:
        try:
            from server.workspace import connect_workspace

            connect_workspace(dest)
            suffix = " Opened as workspace."
        except Exception as e:
            suffix = f" (clone succeeded but workspace connect failed: {e})"

    return True, f"Cloned {url} to {dest}.{suffix}"


@register_executor("git_clone", read_only=False, concurrent_safe=False)
def _exec_git_clone(tool_input, transcript, current_attachments):
    """Emit an approval request. Actual `git clone` runs only after the
    user clicks Yes (or 'Yes, all cli' is set for the session). Unlike
    every other git_* tool, this one does NOT call _get_git_cwd() — by
    design, it runs without a pre-existing workspace."""
    import json

    tool_input.pop("__session_id__", "")
    url = (tool_input.get("url") or "").strip()
    if not url:
        return "Error: url is required."
    payload = json.dumps(
        {
            "action": "git_clone",
            "url": url,
            "dest": tool_input.get("dest") or "",
            "open": tool_input.get("open", True),
        }
    )
    return f"[WS_APPROVAL]{payload}"


def do_git_add_commit(payload: dict) -> tuple[bool, str]:
    """Execute a previously approved git_add_commit. Returns (ok, output_or_error)."""
    cwd, err = _get_git_cwd()
    if err:
        return False, err
    session_id = payload.get("session_id", "")
    files = payload.get("files", []) or []
    message = payload.get("message", "")
    stage_all = payload.get("all", False)

    if not message:
        return False, "commit message is required"

    if files:
        secrets = contains_secret_files(files)
        if secrets:
            return False, f"refusing to commit potential secret files: {', '.join(secrets)}"
        stdout, stderr, rc = _git(["add", *files], cwd)
        if rc != 0:
            return False, f"staging failed: {stderr.strip()}"
    elif stage_all:
        stdout, stderr, rc = _git(["diff", "--cached", "--name-only"], cwd)
        stdout2, _, _ = _git(["diff", "--name-only"], cwd)
        all_files = [f for f in (stdout.strip() + "\n" + stdout2.strip()).split("\n") if f.strip()]
        secrets = contains_secret_files(all_files)
        if secrets:
            return False, f"refusing to stage potential secret files: {', '.join(secrets)}"
        stdout, stderr, rc = _git(["add", "-A"], cwd)
        if rc != 0:
            return False, f"staging failed: {stderr.strip()}"
    else:
        stdout, stderr, rc = _git(["diff", "--cached", "--quiet"], cwd)
        if rc == 0:
            return False, "no files specified and nothing staged"

    stdout, stderr, rc = _git(["commit", "-m", message], cwd)
    if rc != 0:
        return False, f"commit failed: {stderr.strip()}"
    track_git_operations(f'git commit -m "{message}"', stdout + stderr, session_id)
    branch = get_branch(cwd)
    return True, f"Committed on branch {branch}: {message}\n\n{stdout.strip()}"


@register_executor("git_add_commit", read_only=False, concurrent_safe=False)
def _exec_git_add_commit(tool_input, transcript, current_attachments):
    """Emit an approval request — the actual commit only runs once the
    user clicks Yes (or the session has pre-approved 'cli'). Mirrors how
    ws_write_file routes through the approval system."""
    import json

    session_id = tool_input.pop("__session_id__", "")
    message = tool_input.get("message", "")
    files = tool_input.get("files", []) or []
    stage_all = tool_input.get("all", False)
    if not message:
        return "Error: commit message is required."
    payload = json.dumps(
        {
            "action": "git_add_commit",
            "message": message,
            "files": files,
            "all": stage_all,
            "session_id": session_id,
        }
    )
    return f"[WS_APPROVAL]{payload}"


def do_git_push(payload: dict) -> tuple[bool, str]:
    cwd, err = _get_git_cwd()
    if err:
        return False, err
    session_id = payload.get("session_id", "")
    branch = payload.get("branch") or get_branch(cwd)
    set_upstream = payload.get("set_upstream", True)
    args = ["push"]
    if set_upstream:
        args.extend(["-u", "origin", branch])
    else:
        args.extend(["origin", branch])
    stdout, stderr, rc = _git(args, cwd, timeout=30)
    if rc != 0:
        return False, f"push failed: {stderr.strip()}"
    track_git_operations(f"git push -u origin {branch}", stdout + stderr, session_id)
    return True, f"Pushed to origin/{branch}\n\n{(stdout + stderr).strip()}"


@register_executor("git_push", read_only=False, concurrent_safe=False)
def _exec_git_push(tool_input, transcript, current_attachments):
    import json

    session_id = tool_input.pop("__session_id__", "")
    branch = tool_input.get("branch") or ""
    set_upstream = tool_input.get("set_upstream", True)
    payload = json.dumps(
        {
            "action": "git_push",
            "branch": branch,
            "set_upstream": set_upstream,
            "session_id": session_id,
        }
    )
    return f"[WS_APPROVAL]{payload}"


def do_git_create_branch(payload: dict) -> tuple[bool, str]:
    cwd, err = _get_git_cwd()
    if err:
        return False, err
    name = payload.get("name", "")
    from_ref = payload.get("from_ref")
    if not name:
        return False, "branch name is required"
    args = ["checkout", "-b", name]
    if from_ref:
        args.append(from_ref)
    stdout, stderr, rc = _git(args, cwd)
    if rc != 0:
        return False, f"create-branch failed: {stderr.strip()}"
    return True, f"Created and switched to branch '{name}'\n\n{(stdout + stderr).strip()}"


@register_executor("git_create_branch", read_only=False, concurrent_safe=False)
def _exec_git_create_branch(tool_input, transcript, current_attachments):
    import json

    session_id = tool_input.pop("__session_id__", "")
    name = tool_input.get("name", "")
    from_ref = tool_input.get("from_ref")
    if not name:
        return "Error: branch name is required."
    payload = json.dumps(
        {
            "action": "git_create_branch",
            "name": name,
            "from_ref": from_ref,
            "session_id": session_id,
        }
    )
    return f"[WS_APPROVAL]{payload}"


def do_git_push_pr(payload: dict) -> tuple[bool, str]:
    cwd, err = _get_git_cwd()
    if err:
        return False, err
    session_id = payload.get("session_id", "")
    title = payload.get("title", "")
    body = payload.get("body", "")
    base = payload.get("base")
    if not title:
        return False, "PR title is required"

    branch = get_branch(cwd)
    default_branch = get_default_branch(cwd)
    if branch == default_branch:
        return False, (
            f"on default branch '{default_branch}' — create a new branch first with git_create_branch before pushing a PR"
        )

    stdout, stderr, rc = _git(["push", "-u", "origin", branch], cwd, timeout=30)
    push_output = (stdout + stderr).strip()
    if rc != 0:
        return False, f"push failed: {stderr.strip()}"
    track_git_operations(f"git push -u origin {branch}", stdout + stderr, session_id)

    gh_args = ["gh", "pr", "create", "--title", title]
    if body:
        gh_args.extend(["--body", body])
    if base:
        gh_args.extend(["--base", base])
    try:
        result = subprocess.run(
            gh_args,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return True, (
            f"Pushed to origin/{branch} successfully.\n\n{push_output}\n\n"
            f"PR creation skipped: 'gh' CLI not found. Install it from https://cli.github.com/"
        )
    except Exception as e:
        return True, (
            f"Pushed to origin/{branch} successfully.\n\n{push_output}\n\nPR creation failed: {e}"
        )

    pr_output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        return True, (
            f"Pushed to origin/{branch} successfully.\n\n{push_output}\n\n"
            f"But PR creation failed: {result.stderr.strip()}\n"
            f"You can create the PR manually or ensure 'gh' CLI is installed and authenticated."
        )
    track_git_operations(
        f'gh pr create --title "{title}"', result.stdout + result.stderr, session_id
    )
    return True, f"Pushed to origin/{branch} and created PR:\n\n{pr_output}"


@register_executor("git_push_pr", read_only=False, concurrent_safe=False)
def _exec_git_push_pr(tool_input, transcript, current_attachments):
    import json

    session_id = tool_input.pop("__session_id__", "")
    title = tool_input.get("title", "")
    body = tool_input.get("body", "")
    base = tool_input.get("base")
    if not title:
        return "Error: PR title is required."
    payload = json.dumps(
        {
            "action": "git_push_pr",
            "title": title,
            "body": body,
            "base": base,
            "session_id": session_id,
        }
    )
    return f"[WS_APPROVAL]{payload}"


def do_git_checkout(payload: dict) -> tuple[bool, str]:
    cwd, err = _get_git_cwd()
    if err:
        return False, err
    branch = payload.get("branch", "")
    if not branch:
        return False, "branch name is required"
    stdout, stderr, rc = _git(["checkout", branch], cwd)
    if rc != 0:
        return False, f"checkout failed: {stderr.strip()}"
    return True, f"Switched to branch '{branch}'\n\n{(stdout + stderr).strip()}"


@register_executor("git_checkout", read_only=False, concurrent_safe=False)
def _exec_git_checkout(tool_input, transcript, current_attachments):
    import json

    session_id = tool_input.pop("__session_id__", "")
    branch = tool_input.get("branch", "")
    if not branch:
        return "Error: branch name is required."
    payload = json.dumps({"action": "git_checkout", "branch": branch, "session_id": session_id})
    return f"[WS_APPROVAL]{payload}"


def do_git_delete_branch(payload: dict) -> tuple[bool, str]:
    cwd, err = _get_git_cwd()
    if err:
        return False, err
    branch = payload.get("branch", "")
    force = payload.get("force", False)
    if not branch:
        return False, "branch name is required"
    flag = "-D" if force else "-d"
    stdout, stderr, rc = _git(["branch", flag, branch], cwd)
    if rc != 0:
        return False, f"delete-branch failed: {stderr.strip()}"
    return True, f"Deleted branch '{branch}'.\n{(stdout + stderr).strip()}"


@register_executor("git_delete_branch", read_only=False, concurrent_safe=False)
def _exec_git_delete_branch(tool_input, transcript, current_attachments):
    import json

    session_id = tool_input.pop("__session_id__", "")
    branch = tool_input.get("branch", "")
    force = tool_input.get("force", False)
    if not branch:
        return "Error: branch name is required."
    payload = json.dumps(
        {
            "action": "git_delete_branch",
            "branch": branch,
            "force": force,
            "session_id": session_id,
        }
    )
    return f"[WS_APPROVAL]{payload}"


def do_git_merge(payload: dict) -> tuple[bool, str]:
    cwd, err = _get_git_cwd()
    if err:
        return False, err
    branch = payload.get("branch", "")
    no_ff = payload.get("no_ff", False)
    if not branch:
        return False, "branch name is required"
    args = ["merge"]
    if no_ff:
        args.append("--no-ff")
    args.append(branch)
    stdout, stderr, rc = _git(args, cwd)
    if rc != 0:
        return False, f"merge failed: {stderr.strip()}\n\n{stdout.strip()}"
    return True, f"Merged '{branch}' into current branch.\n\n{(stdout + stderr).strip()}"


@register_executor("git_merge", read_only=False, concurrent_safe=False)
def _exec_git_merge(tool_input, transcript, current_attachments):
    import json

    session_id = tool_input.pop("__session_id__", "")
    branch = tool_input.get("branch", "")
    no_ff = tool_input.get("no_ff", False)
    if not branch:
        return "Error: branch name is required."
    payload = json.dumps(
        {
            "action": "git_merge",
            "branch": branch,
            "no_ff": no_ff,
            "session_id": session_id,
        }
    )
    return f"[WS_APPROVAL]{payload}"


def do_git_stash(payload: dict) -> tuple[bool, str]:
    cwd, err = _get_git_cwd()
    if err:
        return False, err
    action = payload.get("stash_action", "push")
    message = payload.get("message")
    index = payload.get("index", 0)
    if action == "push":
        args = ["stash", "push"]
        if message:
            args.extend(["--message", message])
    elif action == "pop":
        args = ["stash", "pop", f"stash@{{{index}}}"]
    elif action == "apply":
        args = ["stash", "apply", f"stash@{{{index}}}"]
    elif action == "drop":
        args = ["stash", "drop", f"stash@{{{index}}}"]
    else:
        return False, f"unknown stash action '{action}'"
    stdout, stderr, rc = _git(args, cwd)
    if rc != 0:
        return False, stderr.strip()
    return True, (stdout + stderr).strip() or f"Stash {action} completed."


@register_executor("git_stash", read_only=False, concurrent_safe=False)
def _exec_git_stash(tool_input, transcript, current_attachments):
    import json

    session_id = tool_input.pop("__session_id__", "")
    payload = json.dumps(
        {
            "action": "git_stash",
            "stash_action": tool_input.get("action", "push"),
            "message": tool_input.get("message"),
            "index": tool_input.get("index", 0),
            "session_id": session_id,
        }
    )
    return f"[WS_APPROVAL]{payload}"
