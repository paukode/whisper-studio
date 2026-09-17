"""Approval executor bodies — the thin async wrappers each ApprovalSpec runs.

Split out of bootstrap.py (which keeps register_defaults and the spec
declarations) so both stay under the repo's per-file line budget. Each function
delegates to the sibling executor/endpoint logic; the frontend never knows which
one runs — it POSTs {action, payload} to /api/approval/execute.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess

from .spec import ApprovalOutcome, refuse_if_agent, refuse_if_agent_rm


async def _do_write(payload: dict) -> ApprovalOutcome:
    from server import workspace

    ws = workspace.get_workspace_path()
    if not ws:
        return ApprovalOutcome(ok=False, error="No workspace connected")
    path = payload.get("path", "")
    content = workspace._normalize_lf(payload.get("content", ""))
    full = os.path.join(ws, path)
    if not workspace._ws_validate_path(full, ws):
        return ApprovalOutcome(ok=False, error="Invalid path")
    if os.path.isfile(full):
        try:
            with open(full, errors="replace") as f:
                workspace.WORKSPACE_BACKUPS[path] = f.read()
        except Exception:
            pass
    try:
        workspace._atomic_write_text(full, content)
    except Exception as e:
        return ApprovalOutcome(ok=False, error=f"Write failed: {e}")
    from server.index.citations import created_file_link

    link = created_file_link(path, full)
    return ApprovalOutcome(
        ok=True,
        output=(
            f"Wrote {path} ({len(content)} bytes). "
            f"To reference this file for the user, copy this link verbatim: {link}"
        ),
    )


async def _do_folder_access(payload: dict) -> ApprovalOutcome:
    """Persist an ask-once grant for a folder OUTSIDE the connected workspace.

    Approving this is the user saying "yes, this app may read that folder" —
    recorded permanently (server/security/folder_grants.py) so the same
    folder is never asked about again. Refused outright for credential
    stores and for any ancestor of one, no matter what the prompt said:
    that refusal is the whole reason a prompt-injected "approve my ~/.ssh
    access" can't become a durable grant.

    Never auto-approvable by an unattended agent — granting the app new
    filesystem reach is exactly the decision that needs a human.
    """
    refusal = refuse_if_agent(payload, what="Granting access to a new folder")
    if refusal:
        return refusal
    from server.security.folder_grants import _canonical, grant, is_sensitive

    raw = payload.get("path") or ""
    real = _canonical(raw)
    if not real:
        return ApprovalOutcome(ok=False, error="No folder path given — nothing to grant.")

    # Refuse protected paths BEFORE creating anything. grant() would reject
    # them anyway, but only after makedirs had already put a directory inside
    # a credential store.
    if is_sensitive(real):
        return ApprovalOutcome(ok=False, error=f"{real} is protected and can never be granted.")

    # ws_open_folder has always been allowed to create the folder it opens, and
    # that is what the user just approved. Create it BEFORE granting so grant()
    # keeps its own rule (never pre-authorize a path that does not exist) for
    # every other caller.
    if payload.get("create") and not os.path.isdir(real):
        try:
            os.makedirs(real, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            return ApprovalOutcome(ok=False, error=f"Could not create folder: {e}")

    ok, err = grant(raw)
    if not ok:
        return ApprovalOutcome(ok=False, error=err)

    # Finish the action the user actually approved. The card says "open this
    # folder as the workspace", so recording the grant and stopping there left
    # the workspace disconnected — the user clicked Approve and nothing opened.
    # Connecting here also makes /api/approval/execute's before/after diff fire
    # ws_folder_opened, so the panel switches to the new root.
    if payload.get("connect"):
        from server.workspace.executors import open_folder_now

        return ApprovalOutcome(ok=True, output=open_folder_now(real))

    # Report the CANONICAL path, not the raw input — the user should see the
    # folder that was actually recorded, not "~/x" or a relative spelling.
    return ApprovalOutcome(ok=True, output=f"Access granted to {real}")


async def _do_delete(payload: dict) -> ApprovalOutcome:
    from server import workspace

    ws = workspace.get_workspace_path()
    if not ws:
        return ApprovalOutcome(ok=False, error="No workspace connected")
    path = payload.get("path", "")
    full = os.path.join(ws, path)
    if not workspace._ws_validate_path(full, ws) or not os.path.exists(full):
        return ApprovalOutcome(ok=False, error="Path not found")
    try:
        if os.path.isfile(full):
            try:
                with open(full, errors="replace") as f:
                    workspace.WORKSPACE_BACKUPS[path] = f.read()
            except Exception:
                pass
            os.remove(full)
        elif os.path.isdir(full):
            shutil.rmtree(full)
    except Exception as e:
        return ApprovalOutcome(ok=False, error=f"Delete failed: {e}")
    return ApprovalOutcome(ok=True, output=f"Deleted {path}")


async def _do_save_to_path(payload: dict) -> ApprovalOutcome:
    """Move a STAGED, already-built file to a caller-given absolute path.

    The file-saving tools (save_file, ws_create_file without a workspace,
    create_docx/pptx/xlsx/pdf) build their full content at tool-call time
    and stage it in the app sandbox (server/workspace/paths.py's
    stage_file_bytes); approving here just moves the ready file into place.
    Unlike _do_write/_do_create above, this does NOT require a connected
    workspace — get_workspace_path() is never consulted.

    Re-validates everything here (not just in the tool executors) — this is
    the single hub every approval executes through, and the payload could
    in principle reach it directly via POST /api/approval/execute. In
    particular, staged_path MUST be inside the staging root: without that
    gate a forged payload could 'move' any readable file on disk into a
    user-visible folder."""
    import shutil

    from server.security.sensitive_paths import is_sensitive_path
    from server.workspace.paths import is_staged_path
    from server.workspace.state import record_save_location

    dest = payload.get("path", "")
    if not dest or not os.path.isabs(dest):
        return ApprovalOutcome(ok=False, error="destination path must be an absolute path")
    real_dest = os.path.realpath(dest)
    if is_sensitive_path(real_dest):
        return ApprovalOutcome(ok=False, error="Refusing to write to a sensitive path")

    staged = payload.get("staged_path", "")
    if not staged or not is_staged_path(staged):
        return ApprovalOutcome(ok=False, error="staged_path is not a staged file")
    real_staged = os.path.realpath(staged)
    if not os.path.isfile(real_staged):
        return ApprovalOutcome(
            ok=False,
            error="The staged file no longer exists (staging entries expire after 24h) — re-run the tool.",
        )

    size = os.path.getsize(real_staged)
    try:
        os.makedirs(os.path.dirname(real_dest), exist_ok=True)
        shutil.move(real_staged, real_dest)
    except Exception as e:
        return ApprovalOutcome(ok=False, error=f"Move failed: {e}")
    # Best-effort cleanup of the now-empty per-file staging subdirectory.
    try:
        os.rmdir(os.path.dirname(real_staged))
    except OSError:
        pass
    record_save_location(os.path.dirname(real_dest))
    from server.index.citations import created_file_link
    from server.workspace.state import REVISION_HINT

    link = created_file_link(os.path.basename(real_dest), real_dest)
    return ApprovalOutcome(
        ok=True,
        output=(
            f"Saved {os.path.basename(real_dest)} to {real_dest} ({size} bytes). "
            f"To reference this file for the user, copy this link verbatim: {link} "
            f"{REVISION_HINT}"
        ),
    )


async def _do_create_document(payload: dict) -> ApprovalOutcome:
    """Write a create_docx/pptx/xlsx/pdf tool's generated bytes into the
    connected workspace. Re-resolves the workspace root at click time (same
    as _do_write) rather than trusting a full path computed when the tool
    was called — the workspace could in principle have changed in between."""
    import base64

    from server import workspace

    ws = workspace.get_workspace_path()
    if not ws:
        return ApprovalOutcome(ok=False, error="No workspace connected")
    path = payload.get("path", "")
    full = os.path.join(ws, path)
    if not workspace._ws_validate_path(full, ws):
        return ApprovalOutcome(ok=False, error="Invalid path")
    try:
        data = base64.b64decode(payload.get("content_b64", ""))
    except Exception as e:
        return ApprovalOutcome(ok=False, error=f"Could not decode content: {e}")
    try:
        workspace._atomic_write_bytes(full, data)
    except Exception as e:
        return ApprovalOutcome(ok=False, error=f"Write failed: {e}")
    from server.index.citations import created_file_link
    from server.workspace.state import REVISION_HINT

    link = created_file_link(path, full)
    return ApprovalOutcome(
        ok=True,
        output=(
            f"Created {path} ({len(data)} bytes). "
            f"To reference this file for the user, copy this link verbatim: {link} "
            f"{REVISION_HINT}"
        ),
    )


async def _do_enter_worktree(payload: dict) -> ApprovalOutcome:
    """Create / resume a worktree for the current chat session and switch in."""
    from server import workspace
    from server.git.worktree_session import enter_worktree, validate_worktree_slug

    ws = workspace.get_workspace_path()
    if not ws:
        return ApprovalOutcome(ok=False, error="No workspace connected")
    slug = (payload.get("name") or "").strip()
    session_id = (payload.get("session_id") or "").strip()
    if not session_id:
        return ApprovalOutcome(ok=False, error="session_id is required")
    try:
        validate_worktree_slug(slug)
    except ValueError as e:
        return ApprovalOutcome(ok=False, error=str(e))
    try:
        session = enter_worktree(ws, slug, session_id)
    except Exception as e:
        return ApprovalOutcome(ok=False, error=str(e))
    return ApprovalOutcome(
        ok=True,
        output=f"Entered worktree {session.worktree_name} at {session.worktree_path} (branch {session.worktree_branch})",
    )


async def _do_exit_worktree(payload: dict) -> ApprovalOutcome:
    from server.git.worktree_session import exit_worktree

    session_id = (payload.get("session_id") or "").strip()
    if not session_id:
        return ApprovalOutcome(ok=False, error="session_id is required")
    force = bool(payload.get("force"))
    try:
        session = exit_worktree(session_id, force=force)
    except Exception as e:
        return ApprovalOutcome(ok=False, error=str(e))
    return ApprovalOutcome(
        ok=True,
        output=f"Exited worktree {session.worktree_name}; cwd restored to {session.original_cwd}",
    )


# ── Git write executors ──────────────────────────────────────────


async def _do_git_clone(payload: dict) -> ApprovalOutcome:
    from server.git.executor import do_git_clone

    ok, output = do_git_clone(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


async def _do_run_tool_script(payload: dict) -> ApprovalOutcome:
    from server.executors.tool_script import do_run_tool_script

    ok, output = await do_run_tool_script(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


async def _do_terminal_send(payload: dict) -> ApprovalOutcome:
    from server.executors.terminal_run import do_terminal_send

    ok, output = await do_terminal_send(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


async def _do_terminal_close(payload: dict) -> ApprovalOutcome:
    from server.executors.terminal_run import do_terminal_close

    ok, output = await do_terminal_close(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


async def _do_terminal_run(payload: dict) -> ApprovalOutcome:
    refusal = refuse_if_agent_rm(payload)
    if refusal:
        return refusal
    from server.executors.terminal_run import do_terminal_run

    ok, output = await do_terminal_run(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


async def _do_preview_start(payload: dict) -> ApprovalOutcome:
    from server.preview.manager import start_preview_session

    ok, output = await start_preview_session(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


async def _do_preview_stop(payload: dict) -> ApprovalOutcome:
    from server.preview.manager import stop_preview_session

    ok, output = await stop_preview_session(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


async def _do_preview_navigate(payload: dict) -> ApprovalOutcome:
    from server.preview.manager import navigate_in_preview

    ok, output = await navigate_in_preview(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


async def _do_preview_click(payload: dict) -> ApprovalOutcome:
    from server.preview.manager import click_in_preview

    ok, output = await click_in_preview(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


async def _do_preview_fill(payload: dict) -> ApprovalOutcome:
    from server.preview.manager import fill_in_preview

    ok, output = await fill_in_preview(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


async def _do_preview_eval(payload: dict) -> ApprovalOutcome:
    from server.preview.manager import eval_in_preview

    ok, output = await eval_in_preview(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


async def _do_preview_resize(payload: dict) -> ApprovalOutcome:
    from server.preview.manager import resize_preview

    ok, output = await resize_preview(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


async def _do_git_add_commit(payload: dict) -> ApprovalOutcome:
    from server.git.executor import do_git_add_commit

    ok, output = do_git_add_commit(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


async def _do_git_push(payload: dict) -> ApprovalOutcome:
    from server.git.executor import do_git_push

    ok, output = do_git_push(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


async def _do_git_pull(payload: dict) -> ApprovalOutcome:
    from server.git.executor import do_git_pull

    ok, output = do_git_pull(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


async def _do_git_create_branch(payload: dict) -> ApprovalOutcome:
    from server.git.executor import do_git_create_branch

    ok, output = do_git_create_branch(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


async def _do_git_push_pr(payload: dict) -> ApprovalOutcome:
    from server.git.executor import do_git_push_pr

    ok, output = do_git_push_pr(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


async def _do_github(payload: dict) -> ApprovalOutcome:
    from server.git.gh_executor import do_github

    ok, output = do_github(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


async def _do_github_api_write(payload: dict) -> ApprovalOutcome:
    from server.git.gh_executor import do_github_api_write

    ok, output = do_github_api_write(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


def _gh_summary(p: dict) -> str:
    args = p.get("args") or []
    head = " ".join(str(a) for a in args[:3])
    return f"GitHub: gh {head}" if head else "GitHub command"


def _gh_render(p: dict) -> str:
    return "gh " + shlex.join(str(a) for a in (p.get("args") or []))


def _gh_api_summary(p: dict) -> str:
    return f"GitHub API: {p.get('method') or 'POST'} {p.get('endpoint', '?')}"


def _gh_api_render(p: dict) -> str:
    method = p.get("method") or "POST"
    return f"gh api --method {method} {p.get('endpoint', '?')}"


async def _do_git_checkout(payload: dict) -> ApprovalOutcome:
    from server.git.executor import do_git_checkout

    ok, output = do_git_checkout(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


async def _do_git_delete_branch(payload: dict) -> ApprovalOutcome:
    from server.git.executor import do_git_delete_branch

    ok, output = do_git_delete_branch(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


async def _do_git_merge(payload: dict) -> ApprovalOutcome:
    from server.git.executor import do_git_merge

    ok, output = do_git_merge(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


async def _do_git_stash(payload: dict) -> ApprovalOutcome:
    from server.git.executor import do_git_stash

    ok, output = do_git_stash(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


async def _do_run_python(payload: dict) -> ApprovalOutcome:
    from server.executors.code import do_run_python

    ok, output = do_run_python(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


async def _do_aws_cli(payload: dict) -> ApprovalOutcome:
    from server.executors.code import do_aws_cli

    ok, output = do_aws_cli(payload)
    return ApprovalOutcome(ok=ok, output=output if ok else None, error=None if ok else output)


async def _do_command(payload: dict) -> ApprovalOutcome:
    """Run a shell command in the workspace. Mirrors ws_shell_endpoint but
    without the request/response plumbing. user_approved is implicit — the
    user just approved via the banner."""
    refusal = refuse_if_agent_rm(payload)
    if refusal:
        return refusal
    from server import workspace
    from server.cwd_tracker import (
        extract_cwd_from_output,
        get_cwd,
        update_cwd,
        wrap_command_for_cwd,
    )
    from server.sandbox import run_sandboxed

    ws = workspace.get_workspace_path()
    if not ws:
        return ApprovalOutcome(ok=False, error="No workspace connected")
    command = (payload.get("command") or "").strip()
    if not command:
        return ApprovalOutcome(ok=False, error="No command")
    # Same validation gate as /api/workspace/shell: the sandbox alone does not
    # stop an approved command from e.g. reading ~/.ssh, so the dangerous-pattern
    # and sensitive-path checks run here too.
    warning = workspace._validate_command(command)
    if warning:
        return ApprovalOutcome(ok=False, error=warning)
    session_id = payload.get("session_id", "")
    effective_cwd = get_cwd(session_id, ws) if session_id else ws
    redirected = (
        workspace._apply_stdin_redirect(command)
        if workspace._needs_stdin_redirect(command)
        else command
    )
    exec_command = wrap_command_for_cwd(redirected)
    try:
        result = run_sandboxed(exec_command, cwd=effective_cwd, timeout=120)
    except subprocess.TimeoutExpired:
        return ApprovalOutcome(ok=False, error="Command timed out")
    except Exception as e:
        return ApprovalOutcome(ok=False, error=str(e))

    output = ""
    if result.stdout:
        output += result.stdout
    if result.stderr:
        output += ("\n" if output else "") + result.stderr
    clean_output, new_cwd = extract_cwd_from_output(output.strip())
    if session_id and new_cwd and os.path.isdir(new_cwd):
        update_cwd(session_id, new_cwd)
    output = clean_output or ("Done." if result.returncode == 0 else "(no output)")
    output = workspace._truncate_shell_output(output)
    return ApprovalOutcome(
        ok=result.returncode == 0,
        output=output,
        error=None if result.returncode == 0 else f"exit code {result.returncode}",
    )


# ── MCP executors ──────────────────────────────────────────────────


async def _do_mcp_tool_call(payload: dict) -> ApprovalOutcome:
    """Run an MCP tool call the user just approved. Registered under the
    "mcp" category so server.security.permissions.resolve_static_decision's
    dedicated tier (and the per-server approval_mode/tool_overrides config
    in mcp_servers.json) governs it like any other approval-gated tool."""
    from server.mcp import mcp_manager

    tool_key = payload.get("tool_key", "")
    arguments = payload.get("arguments") or {}
    if not tool_key:
        return ApprovalOutcome(ok=False, error="tool_key is required")
    output = await mcp_manager.execute_approved_tool_call(tool_key, arguments)
    if isinstance(output, str) and output.startswith("[MCP Error]"):
        return ApprovalOutcome(ok=False, error=output)
    return ApprovalOutcome(ok=True, output=output)


def _mcp_tool_call_summary(p: dict) -> str:
    return f"MCP: {p.get('server', '?')} → {p.get('tool_name', '?')}"


async def _do_mcp_elicit_respond(payload: dict) -> ApprovalOutcome:
    """Deliver a human's answer to a pending MCP elicitation (an MCP server
    asking for input mid tool-call — see server/mcp.py's
    _build_elicitation_callback). refuse_if_agent is defense in depth here:
    the PRIMARY guard against an unattended subagent auto-answering lives in
    the elicitation callback itself (it declines immediately for a call
    flagged `__agent__`, before any pending record — or approval action —
    ever exists), but this executor is the same generic hub every other
    approval flows through, so it gets the same stamp check as the rest."""
    refusal = refuse_if_agent(payload, what="Answering an MCP elicitation")
    if refusal:
        return refusal
    from server.mcp import mcp_manager

    elicitation_id = (payload.get("elicitation_id") or "").strip()
    response_action = payload.get("response_action", "decline")
    content = payload.get("content")
    if not elicitation_id:
        return ApprovalOutcome(ok=False, error="elicitation_id is required")
    ok = mcp_manager.resolve_elicitation(elicitation_id, response_action, content)
    if not ok:
        return ApprovalOutcome(
            ok=False,
            error="No pending elicitation with that id (it may have already "
            "timed out or been answered).",
        )
    return ApprovalOutcome(ok=True, output=f"Elicitation answered: {response_action}")


def _mcp_elicit_summary(p: dict) -> str:
    return (
        f"MCP elicitation ({p.get('response_action', 'decline')}): {p.get('elicitation_id', '?')}"
    )


# ── Summary builders ──────────────────────────────────────────────────


def _summary_write(p: dict) -> str:
    path = p.get("path", "?")
    if p.get("original") is not None and p.get("content") is not None:
        # Rough +/- count using line difference. The real diff renders
        # client-side; this is just the banner's headline.
        old_lines = len(str(p.get("original") or "").splitlines())
        new_lines = len(str(p.get("content") or "").splitlines())
        delta = new_lines - old_lines
        sign = "+" if delta >= 0 else ""
        return f"Update {path} ({sign}{delta} lines)"
    return f"Write {path}"


def _summary_create(p: dict) -> str:
    return f"Create {p.get('path', '?')}"


def _summary_delete(p: dict) -> str:
    return f"Delete {p.get('path', '?')}"


def _summary_save(p: dict) -> str:
    name = p.get("filename") or os.path.basename(p.get("path", "") or "?")
    return f"Save {name} to {p.get('path', '?')}"


def _summary_create_document(p: dict) -> str:
    fmt = (p.get("format") or "").upper()
    size = p.get("size")
    size_str = f" ({size} bytes)" if isinstance(size, int) else ""
    tag = f" [{fmt}]" if fmt else ""
    return f"Create {p.get('path', '?')}{size_str}{tag}"


def _summary_command(p: dict) -> str:
    cmd = (p.get("command") or "").strip()
    return cmd if len(cmd) <= 120 else cmd[:117] + "…"
