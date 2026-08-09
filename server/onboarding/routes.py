"""HTTP API for onboarding: one-click import of an existing Claude Code or
Codex CLI project into whisper's native WHISPER.md / folder-skills / MCP
formats. See ``import_external.py`` for the actual translation."""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server.onboarding import import_external

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])

_IMPORTERS = {
    "claude": import_external.import_claude_project,
    "codex": import_external.import_codex_project,
}


def _err(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


def _detect_source_type(source_path: str) -> str | None:
    """Guess which tool a project came from by its marker files, so the UI
    can offer an "auto-detect" option instead of forcing the user to know
    whether whisper calls it "claude" or "codex"."""
    if os.path.isfile(os.path.join(source_path, "CLAUDE.md")) or os.path.isdir(
        os.path.join(source_path, ".claude")
    ):
        return "claude"
    if os.path.isfile(os.path.join(source_path, "AGENTS.md")) or os.path.isdir(
        os.path.join(source_path, ".codex")
    ):
        return "codex"
    return None


@router.post("/import")
async def import_external_project(request: Request):
    body = await request.json()
    source_path = str(body.get("source_path", "") or "").strip()
    workspace_path = str(body.get("workspace_path", "") or "").strip()
    source_type = str(body.get("source_type", "") or "").strip().lower()

    if not source_path:
        return _err("source_path is required")

    if not workspace_path:
        from server.workspace import get_workspace_path

        workspace_path = get_workspace_path() or ""
    if not workspace_path:
        return _err("workspace_path is required (no workspace connected)")

    if not os.path.isdir(source_path):
        return _err(f"source_path does not exist or is not a directory: {source_path}")

    if not source_type:
        source_type = _detect_source_type(source_path) or ""
    if source_type not in _IMPORTERS:
        return _err(
            'could not determine source_type; pass "claude" or "codex" explicitly '
            "(no CLAUDE.md/.claude or AGENTS.md/.codex found at source_path)"
        )

    try:
        summary = _IMPORTERS[source_type](source_path, workspace_path)
    except import_external.ExternalImportError as e:
        return _err(str(e))
    except Exception as e:
        log.warning("external project import failed: %s", e)
        return _err(f"import failed: {e}", 500)

    # Hot-reload skills the same way the existing skill-import routes do, so a
    # newly copied folder skill is usable on the very next message — no
    # restart required.
    from server import skills as _sk

    _sk.SKILLS = _sk.load_skills()
    _sk.rebuild_tools()

    return {"source_type": source_type, **summary}
