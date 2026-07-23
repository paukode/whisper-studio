"""File read/write endpoints for /api/workspace/*.

Read (`/file`, `/source-file`), mutate (`/write`, `/delete`, `/rename`,
`/duplicate`, `/move`, `/copy-file`), and `/undo`. Registered on the shared
package router via decorator side-effects.
"""

import json
import logging
import os

from fastapi import Request
from fastapi.responses import Response

from server import file_state

from .. import router
from ..paths import (
    _WS_BINARY_EXTS,
    _WS_IMAGE_EXTS,
    WORKSPACE_BACKUPS,
    _atomic_write_text,
    _normalize_lf,
    _ws_validate_path,
)
from ..state import get_workspace_path

log = logging.getLogger("whisper-studio")


@router.get("/file")
async def ws_read_file_endpoint(path: str = "", raw: bool = False):
    ws = get_workspace_path()
    if not ws:
        return Response(
            content=json.dumps({"error": "No workspace"}),
            status_code=400,
            media_type="application/json",
        )
    full = os.path.join(ws, path)
    if not _ws_validate_path(full, ws) or not os.path.isfile(full):
        return Response(
            content=json.dumps({"error": "File not found"}),
            status_code=404,
            media_type="application/json",
        )
    ext = os.path.splitext(path)[1].lower()
    # Serve raw file for preview (images, PDFs)
    if raw:
        import mimetypes

        mime = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            data = f.read()
        return Response(content=data, media_type=mime)
    if ext in _WS_BINARY_EXTS:
        if ext in _WS_IMAGE_EXTS:
            file_type = "image"
        elif ext == ".pdf":
            file_type = "pdf"
        elif ext in {".xlsx", ".xls"}:
            file_type = "spreadsheet"
        elif ext in {".docx", ".doc"}:
            file_type = "word"
        elif ext in {".pptx", ".ppt"}:
            file_type = "presentation"
        else:
            file_type = "binary"
        return {"path": path, "binary": True, "type": file_type, "size": os.path.getsize(full)}
    # SVG is text-based, read as text
    try:
        with open(full, errors="replace") as f:
            content = f.read()
        return {"path": path, "content": content, "size": len(content)}
    except Exception as e:
        return Response(
            content=json.dumps({"error": str(e)}), status_code=500, media_type="application/json"
        )


def _resolve_indexed_file(path: str) -> str | None:
    """Resolve a chat 'source' path to an absolute file we're allowed to read.

    Grounded-index citations link files by *absolute* path, and those files can
    live in an indexed folder that is not the connected workspace (or with no
    workspace connected at all). So an absolute path is accepted when it sits
    inside the connected workspace or any known indexed workspace — the same
    trust boundary /reveal uses. A relative path resolves against the connected
    workspace. Returns the realpath of an existing file, or None.
    """
    if not path:
        return None
    if os.path.isabs(path):
        full = os.path.realpath(path)
        roots: list[str] = []
        ws = get_workspace_path()
        if ws:
            roots.append(os.path.realpath(ws))
        try:  # lazy import to avoid a workspace<->index module cycle
            from server.index import store as _index_store

            roots += [os.path.realpath(r) for r in _index_store.list_indexed_workspaces()]
        except Exception:
            pass
        inside = any(full == r or full.startswith(r + os.sep) for r in roots)
        if not inside or not os.path.isfile(full):
            return None
        return full
    ws = get_workspace_path()
    if not ws:
        return None
    full = os.path.join(ws, path)
    if not _ws_validate_path(full, ws) or not os.path.isfile(full):
        return None
    return full


# Rich document types the index extracts text from. For the dock preview we
# re-run that extraction on demand so a cited .docx/.pdf shows its readable
# text, not raw binary bytes.
_SOURCE_DOC_EXTS = {
    ".pdf",
    ".docx",
    ".doc",
    ".pptx",
    ".ppt",
    ".xlsx",
    ".xls",
    ".html",
    ".htm",
    ".epub",
    ".rtf",
    ".odt",
    ".odp",
    ".ods",
}
_SOURCE_MARKDOWN_EXTS = {".md", ".markdown", ".mdx"}
_SOURCE_MAX_BYTES = 25 * 1024 * 1024  # skip extraction for very large files
# Text is rendered unvirtualized client-side (pre / CSV grid / notebook), so
# cap what the JSON branch will inline. Indexed folders can hold huge CSVs/logs
# (the indexer samples them instead of reading whole), and a citation click
# must not freeze the tab.
_SOURCE_TEXT_MAX_BYTES = 5 * 1024 * 1024


# HEAD is declared explicitly (FastAPI doesn't add it to GET routes): the dock
# probes size via HEAD before handing a spreadsheet to the client-side parser.
@router.api_route("/source-file", methods=["GET", "HEAD"])
async def ws_source_file(path: str = "", raw: bool = False):
    """Content for a chat 'source' link opened in the right dock.

    Unlike /file (workspace-relative only), this accepts the absolute paths that
    grounded-index citations use and resolves them against the indexed folders,
    then returns *readable* content: extracted text for documents, raw text for
    code/markdown, and an inline URL (via raw=true) for images."""
    full = _resolve_indexed_file(path)
    if not full:
        return Response(
            content=json.dumps({"error": "File not found in the workspace or indexed folders."}),
            status_code=404,
            media_type="application/json",
        )
    ext = os.path.splitext(full)[1].lower()
    name = os.path.basename(full)

    # Raw byte stream — <img>/<embed> src and the rich viewers' fetch target.
    # FileResponse streams from a threadpool: no whole-file read into memory,
    # no event-loop stall on a large cited file (indexed folders can hold
    # spreadsheets far bigger than anything a workspace tab would open).
    if raw:
        import mimetypes

        from fastapi.responses import FileResponse

        mime = mimetypes.guess_type(full)[0] or "application/octet-stream"
        return FileResponse(full, media_type=mime)

    if ext in _WS_IMAGE_EXTS:
        return {"path": full, "name": name, "kind": "image"}

    if ext in _SOURCE_DOC_EXTS:
        try:
            if os.path.getsize(full) > _SOURCE_MAX_BYTES:
                raise ValueError("file too large to extract")
            import asyncio

            from server.extract import convert_document

            with open(full, "rb") as f:
                data = f.read()
            text = await asyncio.to_thread(convert_document, data, ext, name)
            return {"path": full, "name": name, "kind": "markdown", "content": text}
        except Exception as e:  # noqa: BLE001 — extraction is best-effort
            log.warning("source-file extract failed for %s: %s", full, e)
            return {
                "path": full,
                "name": name,
                "kind": "unsupported",
                "message": "Couldn't extract a preview for this document. "
                "Cmd/Ctrl-click the source link to open it in Finder.",
            }

    if ext in _WS_BINARY_EXTS:  # audio/video/archives/executables/…
        return {
            "path": full,
            "name": name,
            "kind": "unsupported",
            "message": "Preview isn't available for this file type. "
            "Cmd/Ctrl-click the source link to open it in Finder.",
        }

    # Everything else is text (code, markdown, txt, csv, json, …).
    try:
        if os.path.getsize(full) > _SOURCE_TEXT_MAX_BYTES:
            return {
                "path": full,
                "name": name,
                "kind": "unsupported",
                "message": "This file is too large to preview here. "
                "Cmd/Ctrl-click the source link to open it in Finder.",
            }
        with open(full, errors="replace") as f:
            content = f.read()
    except Exception as e:  # noqa: BLE001
        return Response(
            content=json.dumps({"error": str(e)}), status_code=500, media_type="application/json"
        )
    kind = "markdown" if ext in _SOURCE_MARKDOWN_EXTS else "text"
    return {"path": full, "name": name, "kind": kind, "content": content}


@router.post("/write")
async def ws_write_endpoint(request: Request):
    body = await request.json()
    ws = get_workspace_path()
    if not ws:
        return Response(
            content=json.dumps({"error": "No workspace"}),
            status_code=400,
            media_type="application/json",
        )
    path = body.get("path", "")
    content = _normalize_lf(body.get("content", ""))
    session_id = body.get("session_id", "")
    full = os.path.join(ws, path)
    if not _ws_validate_path(full, ws):
        return Response(
            content=json.dumps({"error": "Invalid path"}),
            status_code=403,
            media_type="application/json",
        )
    # Staleness re-check at write time (file may have changed since approval)
    if session_id and os.path.isfile(full):
        allowed, reason = file_state.check_write_allowed(session_id, path, full)
        if not allowed:
            return Response(
                content=json.dumps({"error": reason}),
                status_code=409,
                media_type="application/json",
            )
    if os.path.isfile(full):
        try:
            with open(full, errors="replace") as f:
                WORKSPACE_BACKUPS[path] = f.read()
        except Exception:
            pass
    _atomic_write_text(full, content)
    if session_id:
        file_state.update_after_write(session_id, path, full)
    return {"path": path, "written": True, "size": len(content)}


@router.post("/delete")
async def ws_delete_endpoint(request: Request):
    body = await request.json()
    ws = get_workspace_path()
    if not ws:
        return Response(
            content=json.dumps({"error": "No workspace"}),
            status_code=400,
            media_type="application/json",
        )
    path = body.get("path", "")
    full = os.path.join(ws, path)
    if not _ws_validate_path(full, ws) or not os.path.exists(full):
        return Response(
            content=json.dumps({"error": "File not found"}),
            status_code=404,
            media_type="application/json",
        )
    if os.path.isfile(full):
        try:
            with open(full, errors="replace") as f:
                WORKSPACE_BACKUPS[path] = f.read()
        except Exception:
            pass
        os.remove(full)
    elif os.path.isdir(full):
        import shutil

        shutil.rmtree(full)
    return {"path": path, "deleted": True}


@router.post("/rename")
async def ws_rename_endpoint(request: Request):
    body = await request.json()
    ws = get_workspace_path()
    if not ws:
        return Response(
            content=json.dumps({"error": "No workspace"}),
            status_code=400,
            media_type="application/json",
        )
    old_path = body.get("path", "")
    new_name = body.get("new_name", "").strip()
    if not old_path or not new_name:
        return Response(
            content=json.dumps({"error": "path and new_name required"}),
            status_code=400,
            media_type="application/json",
        )
    if "/" in new_name or "\\" in new_name:
        return Response(
            content=json.dumps({"error": "new_name must be a filename, not a path"}),
            status_code=400,
            media_type="application/json",
        )
    old_full = os.path.join(ws, old_path)
    if not _ws_validate_path(old_full, ws) or not os.path.exists(old_full):
        return Response(
            content=json.dumps({"error": "Source not found"}),
            status_code=404,
            media_type="application/json",
        )
    new_full = os.path.join(os.path.dirname(old_full), new_name)
    if not _ws_validate_path(new_full, ws):
        return Response(
            content=json.dumps({"error": "Invalid target path"}),
            status_code=403,
            media_type="application/json",
        )
    if os.path.exists(new_full):
        return Response(
            content=json.dumps({"error": "Target already exists"}),
            status_code=409,
            media_type="application/json",
        )
    os.rename(old_full, new_full)
    new_rel = os.path.relpath(new_full, ws)
    return {"old_path": old_path, "new_path": new_rel, "renamed": True}


@router.post("/duplicate")
async def ws_duplicate_endpoint(request: Request):
    import shutil

    body = await request.json()
    ws = get_workspace_path()
    if not ws:
        return Response(
            content=json.dumps({"error": "No workspace"}),
            status_code=400,
            media_type="application/json",
        )
    path = body.get("path", "")
    full = os.path.join(ws, path)
    if not _ws_validate_path(full, ws) or not os.path.exists(full):
        return Response(
            content=json.dumps({"error": "Source not found"}),
            status_code=404,
            media_type="application/json",
        )
    base, ext = os.path.splitext(full)
    copy_full = base + " copy" + ext
    n = 2
    while os.path.exists(copy_full):
        copy_full = base + f" copy {n}" + ext
        n += 1
    if os.path.isfile(full):
        shutil.copy2(full, copy_full)
    else:
        shutil.copytree(full, copy_full)
    return {"path": path, "copy_path": os.path.relpath(copy_full, ws), "duplicated": True}


@router.post("/move")
async def ws_move_endpoint(request: Request):
    """Move a file or directory (used for cut+paste)."""
    import shutil

    body = await request.json()
    ws = get_workspace_path()
    if not ws:
        return Response(
            content=json.dumps({"error": "No workspace"}),
            status_code=400,
            media_type="application/json",
        )
    source = body.get("source", "")
    dest_dir = body.get("destination_dir", "")
    src_full = os.path.join(ws, source)
    dst_dir_full = os.path.join(ws, dest_dir) if dest_dir else ws
    if not _ws_validate_path(src_full, ws) or not os.path.exists(src_full):
        return Response(
            content=json.dumps({"error": "Source not found"}),
            status_code=404,
            media_type="application/json",
        )
    if not _ws_validate_path(dst_dir_full, ws) or not os.path.isdir(dst_dir_full):
        return Response(
            content=json.dumps({"error": "Destination directory not found"}),
            status_code=404,
            media_type="application/json",
        )
    name = os.path.basename(src_full)
    dst_full = os.path.join(dst_dir_full, name)
    if os.path.exists(dst_full):
        return Response(
            content=json.dumps({"error": "Target already exists"}),
            status_code=409,
            media_type="application/json",
        )
    shutil.move(src_full, dst_full)
    return {"old_path": source, "new_path": os.path.relpath(dst_full, ws), "moved": True}


@router.post("/copy-file")
async def ws_copy_file_endpoint(request: Request):
    """Copy a file or directory to a destination (used for copy+paste)."""
    import shutil

    body = await request.json()
    ws = get_workspace_path()
    if not ws:
        return Response(
            content=json.dumps({"error": "No workspace"}),
            status_code=400,
            media_type="application/json",
        )
    source = body.get("source", "")
    dest_dir = body.get("destination_dir", "")
    src_full = os.path.join(ws, source)
    dst_dir_full = os.path.join(ws, dest_dir) if dest_dir else ws
    if not _ws_validate_path(src_full, ws) or not os.path.exists(src_full):
        return Response(
            content=json.dumps({"error": "Source not found"}),
            status_code=404,
            media_type="application/json",
        )
    if not _ws_validate_path(dst_dir_full, ws) or not os.path.isdir(dst_dir_full):
        return Response(
            content=json.dumps({"error": "Destination directory not found"}),
            status_code=404,
            media_type="application/json",
        )
    name = os.path.basename(src_full)
    dst_full = os.path.join(dst_dir_full, name)
    # Auto-rename on conflict
    if os.path.exists(dst_full):
        base, ext = os.path.splitext(name)
        n = 2
        while os.path.exists(dst_full):
            dst_full = os.path.join(dst_dir_full, f"{base} copy {n}{ext}")
            n += 1
    if os.path.isfile(src_full):
        shutil.copy2(src_full, dst_full)
    else:
        shutil.copytree(src_full, dst_full)
    return {"source": source, "new_path": os.path.relpath(dst_full, ws), "copied": True}


@router.post("/undo")
async def ws_undo_endpoint(request: Request):
    body = await request.json()
    ws = get_workspace_path()
    if not ws:
        return Response(
            content=json.dumps({"error": "No workspace"}),
            status_code=400,
            media_type="application/json",
        )
    path = body.get("path", "")
    if path not in WORKSPACE_BACKUPS:
        return Response(
            content=json.dumps({"error": "No backup for this file"}),
            status_code=404,
            media_type="application/json",
        )
    full = os.path.join(ws, path)
    if not _ws_validate_path(full, ws):
        return Response(
            content=json.dumps({"error": "Invalid path"}),
            status_code=403,
            media_type="application/json",
        )
    original = WORKSPACE_BACKUPS.pop(path)
    _atomic_write_text(full, original)
    return {"path": path, "restored": True}
