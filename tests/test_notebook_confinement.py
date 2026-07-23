"""Notebook tools must stay confined to the connected workspace.

Before the fix, ``execute_notebook_tool`` only checked ``os.path.isabs`` and
``.ipynb``, so notebook_read/notebook_edit could read or WRITE any ``.ipynb``
anywhere on disk (e.g. another user's home). Unlike ws_write_file, the write
path also wrote directly with no approval gate.

These tests pin both fixes:
  * every notebook path is validated with ``_ws_validate_path`` against the
    connected workspace (reads and writes);
  * notebook_edit routes through the ``[WS_APPROVAL]`` write flow instead of
    silently writing to disk.

Style mirrors test_workspace_source_file.py (monkeypatch ``get_workspace_path``)
and test_skills_path_traversal.py (assert nothing escapes the sandbox).
"""

import json

from server import notebook

MINIMAL_NB = {
    "cells": [
        {
            "cell_type": "code",
            "source": "print('hello')",
            "metadata": {},
            "outputs": [],
            "execution_count": None,
            "id": "cell0",
        }
    ],
    "metadata": {"kernelspec": {"language": "python"}},
    "nbformat": 4,
    "nbformat_minor": 5,
}


def _write_nb(path):
    path.write_text(json.dumps(MINIMAL_NB, indent=1), encoding="utf-8")


def _set_ws(monkeypatch, ws):
    # Patch the name as imported into server.notebook, the way the workspace
    # tests patch get_workspace_path where it is used.
    monkeypatch.setattr(notebook, "get_workspace_path", lambda: str(ws))


# --- Read confinement --------------------------------------------------------


def test_read_rejects_path_outside_workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "secret.ipynb"
    _write_nb(outside)
    _set_ws(monkeypatch, ws)

    result = json.loads(
        notebook.execute_notebook_tool("notebook_read", {"notebook_path": str(outside)})
    )
    assert "error" in result
    assert "outside the connected workspace" in result["error"]
    # The file's cells must not leak back to the caller.
    assert "cells" not in result


def test_read_accepts_path_inside_workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    nb_path = ws / "analysis.ipynb"
    _write_nb(nb_path)
    _set_ws(monkeypatch, ws)

    result = json.loads(
        notebook.execute_notebook_tool("notebook_read", {"notebook_path": str(nb_path)})
    )
    assert "error" not in result
    assert result["cell_count"] == 1
    assert result["cells"][0]["source"] == "print('hello')"


# --- Write confinement -------------------------------------------------------


def test_edit_rejects_path_outside_workspace_and_writes_nothing(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "victim.ipynb"
    _write_nb(outside)
    before = outside.read_text(encoding="utf-8")
    _set_ws(monkeypatch, ws)

    result_str = notebook.execute_notebook_tool(
        "notebook_edit",
        {"notebook_path": str(outside), "cell_index": 0, "new_source": "PWNED"},
    )
    result = json.loads(result_str)
    assert "error" in result
    assert "outside the connected workspace" in result["error"]
    # No approval sentinel, and the target file is byte-for-byte unchanged.
    assert not result_str.startswith("[WS_APPROVAL]")
    assert outside.read_text(encoding="utf-8") == before


def test_edit_without_workspace_errors_and_writes_nothing(tmp_path, monkeypatch):
    nb_path = tmp_path / "orphan.ipynb"
    _write_nb(nb_path)
    before = nb_path.read_text(encoding="utf-8")
    monkeypatch.setattr(notebook, "get_workspace_path", lambda: None)

    result_str = notebook.execute_notebook_tool(
        "notebook_edit",
        {"notebook_path": str(nb_path), "cell_index": 0, "new_source": "x"},
    )
    result = json.loads(result_str)
    assert "error" in result
    assert "No workspace connected" in result["error"]
    assert not result_str.startswith("[WS_APPROVAL]")
    assert nb_path.read_text(encoding="utf-8") == before


def test_edit_inside_workspace_returns_approval_sentinel_without_writing(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    nb_path = ws / "analysis.ipynb"
    _write_nb(nb_path)
    before = nb_path.read_text(encoding="utf-8")
    _set_ws(monkeypatch, ws)

    result_str = notebook.execute_notebook_tool(
        "notebook_edit",
        {"notebook_path": str(nb_path), "cell_index": 0, "new_source": "print('edited')"},
    )
    # A valid in-workspace edit must be gated behind the approval flow, not
    # written directly — so it returns the sentinel and leaves the file alone.
    assert result_str.startswith("[WS_APPROVAL]")
    payload = json.loads(result_str[len("[WS_APPROVAL]") :])
    assert payload["action"] == "write"
    assert payload["path"] == "analysis.ipynb"  # relative to workspace root
    # The proposed content carries the edit; disk is still untouched until approve.
    proposed = json.loads(payload["content"])
    assert proposed["cells"][0]["source"] == "print('edited')"
    assert nb_path.read_text(encoding="utf-8") == before


def test_edit_out_of_range_index_errors(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    nb_path = ws / "analysis.ipynb"
    _write_nb(nb_path)
    _set_ws(monkeypatch, ws)

    result_str = notebook.execute_notebook_tool(
        "notebook_edit",
        {"notebook_path": str(nb_path), "cell_index": 99, "new_source": "x"},
    )
    result = json.loads(result_str)
    assert "error" in result
    assert "out of range" in result["error"]
    assert not result_str.startswith("[WS_APPROVAL]")


def test_non_ipynb_path_rejected(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    _set_ws(monkeypatch, ws)

    result = json.loads(
        notebook.execute_notebook_tool("notebook_read", {"notebook_path": str(ws / "notes.txt")})
    )
    assert "error" in result
    assert ".ipynb" in result["error"]
