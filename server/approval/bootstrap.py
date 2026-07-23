"""Register ApprovalSpecs for the built-in workspace tools.

This module is the hybrid model's "registry collected centrally" half:
specs are declared here next to (but separate from) the workspace tool
implementations, and a single `register_defaults()` call wires them all
in at startup. Adding a new approval-required tool is one new entry
here — no frontend changes, no tool_executor edits.

Each executor is a thin async wrapper around the existing workspace
endpoint logic. The frontend never knows which endpoint actually runs
the work — it just POSTs to /api/approval/execute with {action, payload}.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess

from .registry import register
from .spec import ApprovalOutcome, ApprovalSpec

# ── Executors ─────────────────────────────────────────────────────────


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
    return ApprovalOutcome(ok=True, output=f"Wrote {path} ({len(content)} bytes)")


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


async def _do_terminal_run(payload: dict) -> ApprovalOutcome:
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
    # Same validation gate as /api/workspace/shell — this approval path
    # sandboxes but previously skipped the dangerous-pattern/sensitive-path
    # check, so an approved command could still e.g. read ~/.ssh.
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


def _summary_command(p: dict) -> str:
    cmd = (p.get("command") or "").strip()
    return cmd if len(cmd) <= 120 else cmd[:117] + "…"


# ── Registration ──────────────────────────────────────────────────────


def register_defaults() -> None:
    """Register specs for the built-in workspace tools. Called at startup."""
    # Writes — diff preview, write category
    for action in ("write", "ws_write_file"):
        register(
            action,
            ApprovalSpec(
                category="write",
                preview="diff",
                summary=_summary_write,
                executor=_do_write,
                risk_hint="low",
                # matched_via_normalization is set only by ws_edit_file's fuzzy
                # quote match; ws_write_file never emits it, so build_payload
                # simply omits it there.
                payload_fields=["path", "content", "original", "matched_via_normalization"],
            ),
        )

    for action in ("create", "ws_create_file"):
        register(
            action,
            ApprovalSpec(
                category="write",
                preview="diff",
                summary=_summary_create,
                executor=_do_write,
                risk_hint="low",
                payload_fields=["path", "content"],
            ),
        )

    # Deletes — text preview, delete category
    for action in ("delete", "ws_delete_file"):
        register(
            action,
            ApprovalSpec(
                category="delete",
                preview="text",
                summary=_summary_delete,
                executor=_do_delete,
                risk_hint="medium",
                payload_fields=["path"],
            ),
        )

    # Commands — command preview, cli category
    for action in ("command", "cli", "ws_run_command"):
        register(
            action,
            ApprovalSpec(
                category="cli",
                preview="command",
                summary=_summary_command,
                executor=_do_command,
                risk_hint="medium",
                payload_fields=["command", "cwd"],
            ),
        )

    # Git writes — under the cli category so "Yes for all commands" also
    # covers commits/pushes. The summary is a short verb-phrase; the
    # rendered command lives in payload.command so CommandPreview can
    # show it without duplicating the title.
    def _commit_summary(p: dict) -> str:
        msg = (p.get("message") or "").split("\n", 1)[0]
        if len(msg) > 60:
            msg = msg[:57] + "…"
        files = p.get("files") or []
        if p.get("all"):
            scope = "all staged + tracked changes"
        elif files:
            scope = f"{len(files)} file{'s' if len(files) != 1 else ''}"
        else:
            scope = "staged changes"
        return f"Commit {scope}: {msg}"

    def _clone_summary(p: dict) -> str:
        url = p.get("url") or "?"
        dest = p.get("dest") or "<auto>"
        return f"git clone {url} → {dest}"

    def _clone_command(p: dict) -> str:
        url = p.get("url") or "?"
        dest = p.get("dest") or "<auto>"
        return f"git clone {url} {dest}"

    register(
        "git_clone",
        ApprovalSpec(
            category="cli",
            preview="command",
            summary=_clone_summary,
            executor=_do_git_clone,
            risk_hint="low",
            payload_fields=["url", "dest", "open", "session_id"],
            render_command=_clone_command,
        ),
    )

    def _terminal_run_summary(p: dict) -> str:
        cmd = (p.get("command") or "?").splitlines()[0]
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        mode = p.get("mode") or "sandbox"
        return f"terminal_run [{mode}]: {cmd}"

    def _terminal_run_command(p: dict) -> str:
        return p.get("command") or "?"

    register(
        "terminal_run",
        ApprovalSpec(
            category="cli",
            preview="command",
            summary=_terminal_run_summary,
            # Sandbox runs are reversible per-session; visible runs touch the
            # user's actual terminal so the risk hint nudges slightly higher.
            executor=_do_terminal_run,
            risk_hint="medium",
            payload_fields=["command", "mode", "timeout", "cwd"],
            render_command=_terminal_run_command,
        ),
    )

    def _commit_command(p: dict) -> str:
        return f"git commit -m {(p.get('message') or '')!r}"

    register(
        "git_add_commit",
        ApprovalSpec(
            category="cli",
            preview="command",
            summary=_commit_summary,
            executor=_do_git_add_commit,
            risk_hint="medium",
            payload_fields=["message", "files", "all", "session_id"],
            render_command=_commit_command,
        ),
    )
    register(
        "git_push",
        ApprovalSpec(
            category="cli",
            preview="command",
            summary=lambda p: f"Push branch {p.get('branch') or '(current)'} to origin",
            executor=_do_git_push,
            risk_hint="medium",
            payload_fields=["branch", "set_upstream", "session_id"],
            render_command=lambda p: (
                f"git push -u origin {p.get('branch') or '(current)'}"
                if p.get("set_upstream", True)
                else f"git push origin {p.get('branch') or '(current)'}"
            ),
        ),
    )
    register(
        "git_create_branch",
        ApprovalSpec(
            category="cli",
            preview="command",
            summary=lambda p: f"Create branch {p.get('name', '?')}"
            + (f" from {p['from_ref']}" if p.get("from_ref") else ""),
            executor=_do_git_create_branch,
            risk_hint="low",
            payload_fields=["name", "from_ref", "session_id"],
            render_command=lambda p: f"git checkout -b {p.get('name', '?')}"
            + (f" {p['from_ref']}" if p.get("from_ref") else ""),
        ),
    )
    register(
        "git_push_pr",
        ApprovalSpec(
            category="cli",
            preview="command",
            summary=lambda p: f"Push + open PR: {p.get('title') or '?'}",
            executor=_do_git_push_pr,
            risk_hint="medium",
            payload_fields=["title", "body", "base", "session_id"],
            render_command=lambda p: f"git push && gh pr create --title {(p.get('title') or '')!r}",
        ),
    )
    # GitHub hybrid tools. Routine writes use the ask-by-default "github"
    # category; destructive verbs (merge/delete/rename/archive, API DELETE) use
    # "github-destructive", a separate category so no blanket "cli" approval —
    # nor a blanket "github" approval — ever covers them.
    for _action, _cat, _exec, _summ, _rend in (
        ("github", "github", _do_github, _gh_summary, _gh_render),
        ("github_destructive", "github-destructive", _do_github, _gh_summary, _gh_render),
        ("github_api_write", "github", _do_github_api_write, _gh_api_summary, _gh_api_render),
        (
            "github_api_write_destructive",
            "github-destructive",
            _do_github_api_write,
            _gh_api_summary,
            _gh_api_render,
        ),
    ):
        register(
            _action,
            ApprovalSpec(
                category=_cat,
                preview="command",
                summary=_summ,
                executor=_exec,
                risk_hint="high" if _cat == "github-destructive" else "medium",
                render_command=_rend,
            ),
        )
    register(
        "git_checkout",
        ApprovalSpec(
            category="cli",
            preview="command",
            summary=lambda p: f"Switch to branch {p.get('branch', '?')}",
            executor=_do_git_checkout,
            risk_hint="low",
            payload_fields=["branch", "session_id"],
            render_command=lambda p: f"git checkout {p.get('branch', '?')}",
        ),
    )
    register(
        "git_delete_branch",
        ApprovalSpec(
            category="cli",
            preview="command",
            summary=lambda p: f"Delete branch {p.get('branch', '?')}"
            + (" (force)" if p.get("force") else ""),
            executor=_do_git_delete_branch,
            risk_hint="high",
            payload_fields=["branch", "force", "session_id"],
            render_command=lambda p: f"git branch {'-D' if p.get('force') else '-d'} {p.get('branch', '?')}",
        ),
    )
    register(
        "git_merge",
        ApprovalSpec(
            category="cli",
            preview="command",
            summary=lambda p: f"Merge {p.get('branch', '?')} into current"
            + (" (no-ff)" if p.get("no_ff") else ""),
            executor=_do_git_merge,
            risk_hint="medium",
            payload_fields=["branch", "no_ff", "session_id"],
            render_command=lambda p: f"git merge {'--no-ff ' if p.get('no_ff') else ''}{p.get('branch', '?')}",
        ),
    )
    register(
        "git_stash",
        ApprovalSpec(
            category="cli",
            preview="command",
            summary=lambda p: f"git stash {p.get('stash_action', 'push')}",
            executor=_do_git_stash,
            risk_hint="low",
            payload_fields=["stash_action", "message", "index", "session_id"],
            render_command=lambda p: (
                f"git stash {p.get('stash_action', 'push')}"
                + (
                    f" -m {(p.get('message') or '')!r}"
                    if p.get("stash_action") == "push" and p.get("message")
                    else ""
                )
                + (
                    f" stash@{{{p.get('index', 0)}}}"
                    if p.get("stash_action") in ("pop", "apply", "drop")
                    else ""
                )
            ),
        ),
    )
    register(
        "run_python",
        ApprovalSpec(
            category="cli",
            preview="command",
            summary=lambda p: "Run Python: "
            + (p.get("code") or "").splitlines()[0][:80]
            + ("…" if len((p.get("code") or "").splitlines()[0]) > 80 else ""),
            executor=_do_run_python,
            risk_hint="medium",
            payload_fields=["code"],
            render_command=lambda p: p.get("code") or "",
        ),
    )

    def _aws_summary(p: dict) -> str:
        cmd = (p.get("command") or "").strip()
        # Surface the AWS service + operation, not the full flags
        parts = cmd.split(maxsplit=3)
        if len(parts) >= 3 and parts[0] == "aws":
            return f"aws {parts[1]} {parts[2]}"
        return cmd[:80] + ("…" if len(cmd) > 80 else "")

    register(
        "aws_cli",
        ApprovalSpec(
            category="cli",
            preview="command",
            summary=_aws_summary,
            executor=_do_aws_cli,
            risk_hint="medium",
            payload_fields=["command"],
            render_command=lambda p: p.get("command") or "",
        ),
    )

    # Worktrees — text preview, dedicated category so users can
    # "Yes for all worktree operations" once they trust the flow.
    register(
        "enter_worktree",
        ApprovalSpec(
            category="worktree",
            preview="text",
            summary=lambda p: f"Enter worktree {p.get('name', '?')}",
            executor=_do_enter_worktree,
            risk_hint="low",
            payload_fields=["name", "session_id"],
        ),
    )
    register(
        "exit_worktree",
        ApprovalSpec(
            category="worktree",
            preview="text",
            summary=lambda p: "Exit current worktree",
            executor=_do_exit_worktree,
            risk_hint="low",
            payload_fields=["session_id", "force"],
        ),
    )

    # Live preview — dedicated category so users can "Yes for all preview
    # operations" independently of cli/write. preview_eval carries the
    # highest risk_hint (arbitrary JS in a real page context); the rest
    # scale with how reversible/contained the action is.

    def _preview_start_summary(p: dict) -> str:
        name = p.get("session_name") or "?"
        exe = p.get("runtimeExecutable")
        if exe:
            args = " ".join(str(a) for a in (p.get("runtimeArgs") or []))
            return f"preview_start [{name}]: {exe} {args}".strip()
        return f"preview_start [{name}]: from .whisper/launch.json"

    def _preview_start_command(p: dict) -> str:
        exe = p.get("runtimeExecutable")
        if not exe:
            return f"(resolve '{p.get('session_name', '?')}' from .whisper/launch.json)"
        args = " ".join(str(a) for a in (p.get("runtimeArgs") or []))
        return f"{exe} {args}".strip()

    register(
        "preview_start",
        ApprovalSpec(
            category="preview",
            preview="command",
            summary=_preview_start_summary,
            executor=_do_preview_start,
            risk_hint="medium",
            payload_fields=[
                "session_name",
                "runtimeExecutable",
                "runtimeArgs",
                "port",
                "cwd",
                "url",
            ],
            render_command=_preview_start_command,
        ),
    )

    register(
        "preview_stop",
        ApprovalSpec(
            category="preview",
            preview="text",
            summary=lambda p: f"Stop preview session {p.get('session_name', '?')}",
            executor=_do_preview_stop,
            risk_hint="low",
            payload_fields=["session_name"],
        ),
    )

    register(
        "preview_navigate",
        ApprovalSpec(
            category="preview",
            preview="text",
            summary=lambda p: f"preview_navigate [{p.get('session_name', '?')}]: {p.get('url', '?')}",
            executor=_do_preview_navigate,
            risk_hint="medium",
            payload_fields=["session_name", "url"],
        ),
    )

    def _preview_click_summary(p: dict) -> str:
        name = p.get("session_name") or "?"
        sel = p.get("selector") or "?"
        extra = " (double-click)" if p.get("doubleClick") else ""
        return f"preview_click [{name}]: {sel}{extra}"

    register(
        "preview_click",
        ApprovalSpec(
            category="preview",
            preview="text",
            summary=_preview_click_summary,
            executor=_do_preview_click,
            risk_hint="medium",
            payload_fields=["session_name", "selector", "doubleClick"],
        ),
    )

    def _preview_fill_summary(p: dict) -> str:
        name = p.get("session_name") or "?"
        sel = p.get("selector") or "?"
        val = str(p.get("value") or "")
        shown = val if len(val) <= 40 else val[:37] + "..."
        return f"preview_fill [{name}]: {sel} = {shown!r}"

    def _preview_fill_command(p: dict) -> str:
        return f"fill({p.get('selector', '?')!r}, {p.get('value', '')!r})"

    register(
        "preview_fill",
        ApprovalSpec(
            category="preview",
            preview="command",
            summary=_preview_fill_summary,
            executor=_do_preview_fill,
            risk_hint="medium",
            payload_fields=["session_name", "selector", "value"],
            render_command=_preview_fill_command,
        ),
    )

    def _preview_eval_summary(p: dict) -> str:
        name = p.get("session_name") or "?"
        expr = (p.get("expression") or "").splitlines()[0][:80]
        return f"preview_eval [{name}]: {expr}"

    register(
        "preview_eval",
        ApprovalSpec(
            category="preview",
            preview="command",
            summary=_preview_eval_summary,
            executor=_do_preview_eval,
            risk_hint="high",  # arbitrary JS — the highest-risk single action
            payload_fields=["session_name", "expression"],
            render_command=lambda p: p.get("expression") or "?",  # full, unabridged JS
        ),
    )

    def _preview_resize_summary(p: dict) -> str:
        name = p.get("session_name") or "?"
        preset = p.get("preset")
        if preset:
            return f"preview_resize [{name}]: {preset}"
        return f"preview_resize [{name}]: {p.get('width')}x{p.get('height')}"

    register(
        "preview_resize",
        ApprovalSpec(
            category="preview",
            preview="text",
            summary=_preview_resize_summary,
            executor=_do_preview_resize,
            risk_hint="low",
            payload_fields=["session_name", "preset", "width", "height", "colorScheme"],
        ),
    )


__all__ = ["register_defaults"]
