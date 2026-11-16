"""The code checks a chat turn can end with: ruff on the Python files and the
workspace's ESLint on the JS/TS files the turn changed.

Everything here is behind the ``code_checks`` switch (Settings > Tools and
automation > Code tools), which is off by default. While it is off the app
runs no linter on the assistant's code at all: not after a write
(server/code_tools/after_write.py) and not when a turn ends.

While it is on, a chat turn that changed code files ends with a
``code_checks_offer`` frame, and the chat asks the user whether to run the
checks. A yes calls ``POST /api/code-tools/check``: the app runs the tools
itself, outside the model, and answers with the findings. Only when there are
findings does the chat send them to the model, as the user's next message. A
clean run costs no tokens. Runs nobody watches (agents, scheduled and headless
runs) are never asked: the runner sends the frame for a chat turn only.
"""

import os

from fastapi import APIRouter, Request

from . import eslint, ruff
from .commands import JS_EXTENSIONS, PYTHON_EXTENSIONS

FLAG = "code_checks"

router = APIRouter(prefix="/api/code-tools", tags=["code-tools"])

# The tools that write a file the assistant names by path.
_WRITE_TOOLS = frozenset({"ws_write_file", "ws_edit_file", "ws_create_file", "save_file"})


def enabled() -> bool:
    from server.infrastructure.feature_flags import is_enabled

    return is_enabled(FLAG)


def _checkable(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in (*PYTHON_EXTENSIONS, *JS_EXTENSIONS) and os.path.isfile(path)


def changed_code_files(messages: list, ws: str | None) -> list[str]:
    """The Python and JS/TS files the current turn wrote, as absolute paths in
    the order it first wrote them. Only files that exist now: one the turn
    wrote and then removed has nothing to check."""
    from server.goals.deliverables import call_input, resolve_path, turn_messages

    found: list[str] = []
    for m in turn_messages(messages):
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") not in _WRITE_TOOLS:
                continue
            given = call_input(block)
            named = str(given.get("destination") or given.get("path") or "")
            if not named:
                continue
            resolved = resolve_path(named, ws)
            if not os.path.isabs(resolved):
                continue  # a relative path with no workspace names no file
            full = os.path.realpath(resolved)
            if _checkable(full) and full not in found:
                found.append(full)
    return found


def offer(messages: list, ws: str | None) -> dict | None:
    """The ``code_checks_offer`` frame for a turn that changed code files,
    else None. None also while the switch is off."""
    if not enabled():
        return None
    files = changed_code_files(messages, ws)
    return {"code_checks_offer": {"files": files}} if files else None


def _check_one(ws: str | None, full: str) -> dict:
    root = os.path.realpath(ws) if ws else None
    inside = bool(root) and os.path.commonpath([full, root]) == root
    rel = os.path.relpath(full, root) if inside else os.path.basename(full)
    if os.path.splitext(full)[1].lower() in PYTHON_EXTENSIONS:
        # Outside the workspace ruff reads the config of the file's own folder,
        # as the check after a write does.
        issues, text = ruff.check(root if inside else os.path.dirname(full), full, rel)
        tool = "ruff"
    elif inside:
        issues, text = eslint.check(root, full, rel)
        tool = "eslint"
    else:
        issues, text = None, f"ESLint did not check {rel}: it is outside the connected workspace."
        tool = "eslint"
    return {"path": full, "tool": tool, "issues": issues, "text": text}


def run(ws: str | None, files: list[str]) -> dict:
    """Check ``files`` and answer with one result per file, the total number
    of findings, and ``report``: the message that carries the findings to the
    model (empty when there are none). ``issues`` is None for a file that
    could not be checked; its ``text`` says why."""
    results = [_check_one(ws, f) for f in files]
    found = [r for r in results if r["issues"]]
    total = sum(r["issues"] for r in found)
    report = ""
    if found:
        count = "1 issue" if total == 1 else f"{total} issues"
        report = (
            f"I ran the code checks on the files your last turn changed. They found {count}. "
            "Fix them, and change nothing else.\n\n" + "\n\n".join(r["text"] for r in found)
        )
    return {"results": results, "issues": total, "report": report}


@router.post("/check")
def check_files(body: dict, request: Request):
    """Run the code checks on the files a ``code_checks_offer`` frame named.
    A plain ``def``: FastAPI runs it in the threadpool, so the linters never
    block the event loop. With the switch off nothing runs."""
    if not enabled():
        return {"results": [], "issues": 0, "report": "", "enabled": False}
    from server import workspace

    named = body.get("files") if isinstance(body, dict) else None
    files: list[str] = []
    for f in named if isinstance(named, list) else []:
        if isinstance(f, str) and os.path.isabs(f):
            full = os.path.realpath(f)
            if _checkable(full) and full not in files:
                files.append(full)
    return {**run(workspace.get_workspace_path() or None, files), "enabled": True}
