"""The `office_script` approval: run the approved code, then place the file.

Lives here rather than in server/approval/bootstrap.py because bootstrap is
close to the file-size guardrail (tests/test_file_size_budget.py) and this
handler is self-contained; bootstrap's register_defaults() just calls
register_office_script_approval().

Ordering matters and is the whole point of the design. The script produces
its document inside a throwaway work directory (server/documents/script.py)
and hands the bytes back here; only then does this module decide where they
land, re-validating the destination at click time exactly as
_do_create_document / _do_save_to_path do. A script therefore cannot choose
its own output location, and a failed script writes nothing anywhere.

`office-script` is its own approval category, deliberately not "write" or
"cli": approving a document write should not silently authorize running
code, and approving shell commands should not silently authorize writing
documents. A user who wants to iterate freely can still say yes-to-all for
this category alone.
"""

import os

from server.approval.registry import register
from server.approval.spec import ApprovalOutcome, ApprovalSpec

# The approval card shows the code verbatim; the summary line is the header
# above it, so it stays short.
_SUMMARY_MAX = 90


def _resolve_destination(payload: dict) -> tuple[str, str]:
    """(absolute_destination, error). Re-resolved at click time, never
    trusted from the tool call: the workspace could have changed, and the
    payload can in principle reach /api/approval/execute directly."""
    from server import workspace
    from server.security.sensitive_paths import is_sensitive_path

    path = payload.get("path", "")
    if not path:
        return "", "No destination path in the approval payload"

    if payload.get("dest_kind") == "workspace":
        ws = workspace.get_workspace_path()
        if not ws:
            return "", "No workspace connected"
        full = os.path.join(ws, path)
        if not workspace._ws_validate_path(full, ws):
            return "", "Invalid path"
        return full, ""

    if not os.path.isabs(path):
        return "", "destination path must be an absolute path"
    real = os.path.realpath(path)
    if is_sensitive_path(real):
        return "", "Refusing to write to a sensitive path"
    return real, ""


async def _do_office_script(payload: dict) -> ApprovalOutcome:
    """Run the approved script in the sandbox, then write what it produced."""
    from server.documents.script import run_office_script
    from server.workspace.paths import _atomic_write_bytes

    dest, error = _resolve_destination(payload)
    if error:
        return ApprovalOutcome(ok=False, error=error)

    ok, message, data = run_office_script(
        payload.get("code", ""),
        payload.get("format", ""),
        payload.get("source_path", "") or "",
    )
    if not ok:
        return ApprovalOutcome(ok=False, error=message)

    try:
        _atomic_write_bytes(dest, data)
    except Exception as e:
        return ApprovalOutcome(ok=False, error=f"Write failed: {e}")

    if payload.get("dest_kind") != "workspace":
        # Keeps a file saved outside any workspace clickable/revealable
        # afterwards, the same way _do_save_to_path does.
        from server.workspace.state import record_save_location

        record_save_location(os.path.dirname(dest))

    from server.index.citations import created_file_link
    from server.workspace.state import REVISION_HINT

    label = payload.get("path", "") if payload.get("dest_kind") == "workspace" else dest
    link = created_file_link(os.path.basename(dest), dest)
    output = (
        f"Wrote {label} ({len(data)} bytes). "
        f"To reference this file for the user, copy this link verbatim: {link} "
        f"{REVISION_HINT}"
    )
    if message:
        output += f"\n\nScript output:\n{message}"
    return ApprovalOutcome(ok=True, output=output)


def _summary(payload: dict) -> str:
    summary = (payload.get("summary") or "").strip()
    name = os.path.basename(payload.get("path", "") or "?")
    verb = "Edit" if payload.get("source_path") else "Build"
    head = f"{verb} {name}"
    if summary:
        head = f"{head} — {summary}"
    return head if len(head) <= _SUMMARY_MAX else head[: _SUMMARY_MAX - 1] + "…"


def register_office_script_approval() -> None:
    """Called from server/approval/bootstrap.py's register_defaults()."""
    register(
        "office_script",
        ApprovalSpec(
            category="office-script",
            preview="command",
            summary=_summary,
            executor=_do_office_script,
            risk_hint="medium",
            payload_fields=["path", "dest_kind", "format", "code", "source_path", "summary"],
            render_command=lambda p: p.get("code") or "",
        ),
    )
