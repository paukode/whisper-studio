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

# ── Executors ─────────────────────────────────────────────────────────
from .executors import (
    _do_aws_cli,
    _do_command,
    _do_create_document,
    _do_delete,
    _do_enter_worktree,
    _do_exit_worktree,
    _do_folder_access,
    _do_git_add_commit,
    _do_git_checkout,
    _do_git_clone,
    _do_git_create_branch,
    _do_git_delete_branch,
    _do_git_merge,
    _do_git_push,
    _do_git_push_pr,
    _do_git_stash,
    _do_github,
    _do_github_api_write,
    _do_mcp_elicit_respond,
    _do_mcp_tool_call,
    _do_preview_click,
    _do_preview_eval,
    _do_preview_fill,
    _do_preview_navigate,
    _do_preview_resize,
    _do_preview_start,
    _do_preview_stop,
    _do_run_python,
    _do_run_tool_script,
    _do_save_to_path,
    _do_terminal_close,
    _do_terminal_run,
    _do_terminal_send,
    _do_write,
    _gh_api_render,
    _gh_api_summary,
    _gh_render,
    _gh_summary,
    _mcp_elicit_summary,
    _mcp_tool_call_summary,
    _summary_command,
    _summary_create,
    _summary_create_document,
    _summary_delete,
    _summary_save,
    _summary_write,
)
from .registry import register
from .spec import ApprovalSpec

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

    # create_docx/pptx/xlsx/create_pdf — same trust boundary as ws_create_file
    # (a file inside the already-connected workspace), so it shares the
    # "write" category and benefits from "Yes for all writes" the same way.
    # No diff to show (binary content), so preview="text" just renders the
    # summary line.
    register(
        "create_document",
        ApprovalSpec(
            category="write",
            preview="text",
            summary=_summary_create_document,
            executor=_do_create_document,
            risk_hint="low",
            payload_fields=["path", "content_b64", "format", "size"],
        ),
    )

    # The direct-save move (save_file, ws_create_file without a workspace,
    # document tools) — its own category (not "write") so it neither
    # inherits "Yes for all writes" session-memory from ordinary workspace
    # writes nor triggers the git-changes cache invalidation
    # /api/approval/execute does for the write/delete/cli categories (these
    # files live outside any workspace, so there's no git state to refresh).
    register(
        "save_to_path",
        ApprovalSpec(
            category="save",
            preview="text",
            summary=_summary_save,
            executor=_do_save_to_path,
            risk_hint="low",
            payload_fields=["path", "filename", "staged_path", "size"],
        ),
    )

    # office_script — runs model-written python-docx/pptx/openpyxl code and
    # then writes what it produced. Registered from its own module (see
    # server/documents/approval.py) to keep this file under the size budget.
    from server.documents.approval import register_office_script_approval

    register_office_script_approval()

    # Folder access — its own category so it is never covered by "Yes for all
    # writes"/"all commands". Granting the app reach into a NEW folder outside
    # the connected workspace is a distinct, durable decision (it persists
    # across sessions), so it gets its own prompt and its own session bucket.
    register(
        "folder_access",
        ApprovalSpec(
            category="folder-access",
            preview="text",
            summary=lambda p: f"Allow access to {p.get('path', '?')}",
            executor=_do_folder_access,
            risk_hint="medium",
            payload_fields=["path", "reason", "create", "connect"],
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
        if (p.get("sandbox_permissions") or "").strip() == "danger-full-access":
            reason = (p.get("justification") or "no justification given").strip()
            return f"FULL FILESYSTEM ACCESS — {reason} — terminal_run [{mode}]: {cmd}"
        return f"terminal_run [{mode}]: {cmd}"

    def _terminal_run_command(p: dict) -> str:
        return p.get("command") or "?"

    register(
        "terminal_run",
        ApprovalSpec(
            category="cli",
            preview="command",
            summary=_terminal_run_summary,
            # Sandbox runs are workspace-write confined by default; an
            # escalated (danger-full-access) run is unconfined, so its card
            # says so in the summary and carries the model's justification.
            executor=_do_terminal_run,
            risk_hint="medium",
            payload_fields=[
                "command",
                "mode",
                "timeout",
                "cwd",
                "sandbox_permissions",
                "justification",
            ],
            render_command=_terminal_run_command,
        ),
    )

    def _tool_script_summary(p: dict) -> str:
        code = (p.get("code") or "").strip()
        first = next((ln for ln in code.splitlines() if ln.strip()), "")
        if len(first) > 70:
            first = first[:67] + "..."
        return f"Run tool script: {first or '(empty)'}"

    register(
        "run_tool_script",
        ApprovalSpec(
            category="cli",
            preview="command",
            summary=_tool_script_summary,
            executor=_do_run_tool_script,
            risk_hint="medium",
            payload_fields=["code", "session_id"],
            render_command=lambda p: p.get("code") or "",
        ),
    )

    def _terminal_send_summary(p: dict) -> str:
        first = next((ln for ln in (p.get("input") or "").splitlines() if ln.strip()), "")
        if len(first) > 80:
            first = first[:77] + "..."
        return f"terminal_send: {first or '(read pending output)'}"

    register(
        "terminal_send",
        ApprovalSpec(
            category="cli",
            preview="command",
            summary=_terminal_send_summary,
            executor=_do_terminal_send,
            risk_hint="medium",
            payload_fields=["input", "timeout", "cwd", "session_id"],
            render_command=lambda p: p.get("input") or "(read pending output)",
        ),
    )
    register(
        "terminal_close",
        ApprovalSpec(
            category="cli",
            preview="command",
            summary="Close the interactive terminal",
            executor=_do_terminal_close,
            risk_hint="low",
            payload_fields=["session_id"],
            render_command=lambda p: "terminal_close",
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

    def _run_python_summary(p: dict) -> str:
        first = (p.get("code") or "").splitlines()[0] if (p.get("code") or "") else ""
        label = "Run Python: " + first[:80] + ("…" if len(first) > 80 else "")
        if (p.get("sandbox_permissions") or "").strip() == "danger-full-access":
            reason = (p.get("justification") or "no justification given").strip()
            return f"FULL FILESYSTEM ACCESS — {reason} — {label}"
        return label

    register(
        "run_python",
        ApprovalSpec(
            category="cli",
            preview="command",
            summary=_run_python_summary,
            executor=_do_run_python,
            risk_hint="medium",
            payload_fields=["code", "sandbox_permissions", "justification"],
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

    # MCP tool calls — dedicated "mcp" category so per-server approval_mode /
    # tool_overrides (mcp_servers.json) sit on their own tier instead of
    # falling into "cli" or another generic bucket. Only reached when
    # server.mcp.MCPManager.call_tool decided the tool's tier isn't "auto"
    # (or is "writes" on a tool that looks like a mutation).
    register(
        "mcp_tool_call",
        ApprovalSpec(
            category="mcp",
            preview="text",
            summary=_mcp_tool_call_summary,
            executor=_do_mcp_tool_call,
            risk_hint="medium",
            payload_fields=["tool_key", "arguments", "server", "tool_name"],
        ),
    )

    # MCP elicitations — an MCP server asking the human for input mid tool
    # call. Its own category (not "mcp") since it is answered directly via
    # POST /api/approval/execute from Settings → MCP rather than through the
    # [WS_APPROVAL] pause-a-chat-turn pipeline the rest of this file uses;
    # resolve_static_decision never sees this category.
    register(
        "mcp_elicit_respond",
        ApprovalSpec(
            category="mcp-elicit",
            preview="text",
            summary=_mcp_elicit_summary,
            executor=_do_mcp_elicit_respond,
            risk_hint="medium",
            payload_fields=["elicitation_id", "response_action", "content"],
        ),
    )


__all__ = ["register_defaults"]
