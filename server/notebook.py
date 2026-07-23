"""
NotebookEdit / NotebookRead tools — read and edit Jupyter .ipynb notebooks.
Read and edit Jupyter .ipynb notebooks.
"""

import json
import logging
import os
import uuid

from server.workspace import _ws_validate_path, get_workspace_path

log = logging.getLogger("whisper-studio")

# ── Tool definitions ──────────────────────────────────────────────────────────

NOTEBOOK_READ_TOOL = {
    "name": "notebook_read",
    "description": (
        "Read a Jupyter notebook (.ipynb) and return all cells with their type, "
        "source, and output. Use this before editing to understand the notebook structure."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "notebook_path": {
                "type": "string",
                "description": "Absolute path to the .ipynb file",
            },
        },
        "required": ["notebook_path"],
    },
}

NOTEBOOK_EDIT_TOOL = {
    "name": "notebook_edit",
    "description": (
        "Edit a cell in a Jupyter notebook (.ipynb file). "
        "Supports replace (default), insert (add cell after cell_index), and delete modes. "
        "Always call notebook_read first to understand the current cell structure."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "notebook_path": {
                "type": "string",
                "description": "Absolute path to the .ipynb file",
            },
            "cell_index": {
                "type": "integer",
                "description": "0-based index of the cell to edit/insert-after/delete",
            },
            "new_source": {
                "type": "string",
                "description": "New source content for the cell (not needed for delete)",
            },
            "cell_type": {
                "type": "string",
                "enum": ["code", "markdown"],
                "description": "Cell type — required for insert, defaults to existing type for replace",
            },
            "edit_mode": {
                "type": "string",
                "enum": ["replace", "insert", "delete"],
                "description": "replace (default): replace cell source. insert: add new cell after cell_index. delete: remove cell.",
            },
        },
        "required": ["notebook_path", "cell_index"],
    },
}

NOTEBOOK_TOOLS = [NOTEBOOK_READ_TOOL, NOTEBOOK_EDIT_TOOL]
NOTEBOOK_TOOL_NAMES = {t["name"] for t in NOTEBOOK_TOOLS}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _load_notebook(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _confine_path(path: str) -> tuple[str | None, str | None]:
    """Validate a notebook path before any read or write.

    Requires an absolute ``.ipynb`` path that resolves *inside* the connected
    workspace — the same confinement the ws_* file tools enforce via
    ``_ws_validate_path``. Without this, notebook_read/notebook_edit could
    reach any ``.ipynb`` anywhere on disk (another user's home, etc.).

    Returns ``(workspace_root, None)`` on success or ``(None, error_json)`` on
    any failure, so callers can ``return err`` directly.
    """
    if not path:
        return None, json.dumps({"error": "notebook_path is required"})
    if not os.path.isabs(path):
        return None, json.dumps({"error": "notebook_path must be absolute"})
    if not path.endswith(".ipynb"):
        return None, json.dumps({"error": "Only .ipynb files are supported"})
    ws = get_workspace_path()
    if not ws:
        return None, json.dumps(
            {"error": "No workspace connected. Connect a workspace before using notebook tools."}
        )
    if not _ws_validate_path(path, ws):
        return None, json.dumps(
            {"error": f"notebook_path is outside the connected workspace: {path}"}
        )
    return ws, None


def _cell_summary(cell: dict, idx: int) -> dict:
    source = cell.get("source", "")
    if isinstance(source, list):
        source = "".join(source)
    outputs = cell.get("outputs", [])
    output_text = ""
    for o in outputs[:3]:  # limit output
        if o.get("text"):
            t = o["text"]
            if isinstance(t, list):
                t = "".join(t)
            output_text += t[:500]
        elif o.get("output_type") == "error":
            output_text += f"[Error] {o.get('ename', '')}: {o.get('evalue', '')}"
    return {
        "index": idx,
        "id": cell.get("id", str(idx)),
        "cell_type": cell.get("cell_type", "code"),
        "source": source[:2000],
        "output": output_text[:500] if output_text else None,
    }


# ── Executors ─────────────────────────────────────────────────────────────────


def execute_notebook_tool(tool_name: str, tool_input: dict) -> str:
    path = tool_input.get("notebook_path", "")
    # Confine every notebook path to the connected workspace (applies to reads
    # AND writes — reading an arbitrary .ipynb off disk is itself a leak).
    ws, err = _confine_path(path)
    if err:
        return err

    if tool_name == "notebook_read":
        if not os.path.exists(path):
            return json.dumps({"error": f"File not found: {path}"})
        try:
            nb = _load_notebook(path)
            cells = nb.get("cells", [])
            language = nb.get("metadata", {}).get("kernelspec", {}).get("language", "python")
            return json.dumps(
                {
                    "path": path,
                    "language": language,
                    "cell_count": len(cells),
                    "cells": [_cell_summary(c, i) for i, c in enumerate(cells)],
                }
            )
        except Exception as e:
            return json.dumps({"error": str(e)})

    elif tool_name == "notebook_edit":
        if not os.path.exists(path):
            return json.dumps({"error": f"File not found: {path}"})
        try:
            with open(path, encoding="utf-8") as f:
                original = f.read()
            nb = json.loads(original)
        except Exception as e:
            return json.dumps({"error": str(e)})

        cells = nb.setdefault("cells", [])
        idx = int(tool_input.get("cell_index", 0))
        edit_mode = tool_input.get("edit_mode", "replace")
        new_source = tool_input.get("new_source", "")
        cell_type = tool_input.get("cell_type", "code")

        if edit_mode == "delete":
            if idx < 0 or idx >= len(cells):
                return json.dumps({"error": f"cell_index {idx} out of range (0-{len(cells) - 1})"})
            cells.pop(idx)

        elif edit_mode == "insert":
            new_cell: dict = {
                "cell_type": cell_type,
                "source": new_source,
                "metadata": {},
                "id": uuid.uuid4().hex[:8],
            }
            if cell_type == "code":
                new_cell["outputs"] = []
                new_cell["execution_count"] = None
            insert_at = min(idx + 1, len(cells))
            cells.insert(insert_at, new_cell)

        else:  # replace
            if idx < 0 or idx >= len(cells):
                return json.dumps({"error": f"cell_index {idx} out of range (0-{len(cells) - 1})"})
            existing = cells[idx]
            existing["source"] = new_source
            if cell_type:
                existing["cell_type"] = cell_type
            if existing.get("cell_type") == "code":
                existing["outputs"] = []
                existing["execution_count"] = None

        # Do NOT write directly. Mirror ws_write_file / ws_edit_file: emit a
        # [WS_APPROVAL] payload with the "write" action so the change goes
        # through the same user-approval + diff-preview gate. The path is made
        # relative to the workspace so the approval executor (_do_write) rejoins
        # and re-validates it exactly like the ws_* tools.
        try:
            new_content = json.dumps(nb, indent=1, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)})
        rel_path = os.path.relpath(os.path.realpath(path), os.path.realpath(ws))
        payload = json.dumps(
            {
                "action": "write",
                "path": rel_path,
                "content": new_content,
                "original": original,
            }
        )
        return f"[WS_APPROVAL]{payload}"

    return json.dumps({"error": f"Unknown notebook tool: {tool_name}"})
