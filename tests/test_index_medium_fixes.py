"""Two medium index-subsystem fixes.

(1) The PUT /settings route now includes ``entity_descriptions`` in its patch-key
    allow-list, so the (fully wired, pipeline-consumed) toggle actually persists.
(2) Read paths (index_list / stats / cross-workspace graphs / the launchd agent)
    no longer fabricate an empty active-backend index DB for a folder that was
    indexed under the OTHER embed backend — indexes are per-backend, and opening a
    missing db path with sqlite3 would silently create an empty file.
"""

import asyncio
import os

import numpy as np
import pytest

from server.index import agent, graph_views, paths, routes, scheduler, store, wssettings
from server.index.config import EMBED_DIM


@pytest.fixture(autouse=True)
def _tmp_index_dir(tmp_path, monkeypatch):
    # Redirect index storage into the test's tmp dir so nothing touches storage/.
    monkeypatch.setattr(paths, "INDEX_DATA_DIR", str(tmp_path / "index"))
    # The pending pre-index store is derived from INDEX_DATA_DIR at import time, so
    # it must be redirected explicitly.
    monkeypatch.setattr(wssettings, "_PENDING_PATH", str(tmp_path / "pending.json"))
    # Default the active embed backend to qwen3 (index.db); individual tests flip it.
    from server.infrastructure import model_mode

    monkeypatch.setattr(model_mode, "resolve_backend", lambda cap, config=None: "qwen3")


def _unit(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(EMBED_DIM).astype(np.float32)
    return v / np.linalg.norm(v)


class _FakeRequest:
    """Minimal stand-in for a FastAPI Request — the settings routes only await
    ``.json()``."""

    def __init__(self, payload: dict):
        self._payload = payload

    async def json(self):
        return self._payload


# ── fix (1): entity_descriptions toggle persists through the PUT route ────────


def test_put_settings_persists_entity_descriptions(monkeypatch):
    """PUT /settings with ``entity_descriptions`` now survives the patch-key filter
    and is persisted (previously the key was silently dropped, so the toggle — which
    the indexing pipeline reads — could never be turned on)."""
    # The route re-applies the schedule and syncs the launchd agent; both are I/O
    # side effects unrelated to this assertion.
    monkeypatch.setattr(scheduler, "apply_workspace", lambda ws: None)
    monkeypatch.setattr(agent, "regenerate", lambda: None)

    ws = "/fake/ws-desc-setting"
    # Make it a real (qwen3) index so update_settings writes to the index meta.
    store.replace_file(
        ws,
        "a.md",
        {"hash": "h", "size": 1, "mtime": 1.0},
        [{"start_line": 1, "end_line": 1, "text": "x", "vec": _unit(1), "entities": []}],
    )
    assert wssettings.get_settings(ws)["entity_descriptions"] == {
        "enabled": False,
        "engine": "none",
    }

    req = _FakeRequest({"path": ws, "entity_descriptions": {"enabled": True, "engine": "haiku"}})
    result = asyncio.run(routes.put_index_settings(req))
    assert result["entity_descriptions"] == {"enabled": True, "engine": "haiku"}

    # Persisted: a fresh read reflects the enabled toggle.
    assert wssettings.get_settings(ws)["entity_descriptions"] == {
        "enabled": True,
        "engine": "haiku",
    }


# ── fix (2): reads never fabricate an empty active-backend index DB ───────────


def test_stats_for_unindexed_is_empty_and_creates_no_db():
    """store.stats() on a folder with no active-backend index reports empty and
    does NOT create a db file (opening a missing path would fabricate one)."""
    ws = "/fake/ws-never-indexed"
    assert store.has_index(ws) is False
    s = store.stats(ws)
    assert s["files"] == 0 and s["chunks"] == 0 and s["nodes"] == 0
    assert s["last_indexed_at"] is None
    assert not os.path.exists(store.db_path(ws))  # nothing fabricated


def test_index_list_skips_other_backend_without_fabricating(monkeypatch):
    """A folder indexed ONLY under the cohere backend is discoverable by
    list_indexed_workspaces (any backend) but must not be surfaced — or read — as
    an empty index under the active qwen3 backend, and reading must not create
    index.db."""
    from server.infrastructure import model_mode

    ws = "/fake/ws-cohere-only"
    # Index the folder under cohere only (creates index-cohere.db with a workspace
    # meta row so list_indexed_workspaces can discover it).
    monkeypatch.setattr(model_mode, "resolve_backend", lambda cap, config=None: "cohere")
    store.set_meta(ws, workspace=ws, last_indexed_at="2026-01-01T00:00:00Z")
    assert os.path.exists(paths.db_path(ws, "cohere"))

    # Active backend is qwen3 (index.db), which does NOT exist for this folder.
    monkeypatch.setattr(model_mode, "resolve_backend", lambda cap, config=None: "qwen3")
    assert store.list_indexed_workspaces() == [ws]  # still discoverable
    assert store.has_index(ws) is False  # but not under the active backend

    out = asyncio.run(routes.index_list())
    assert out["indexes"] == []  # skipped, not shown as a zeroed index
    # The read did NOT fabricate an empty active-backend db.
    assert not os.path.exists(paths.db_path(ws, "qwen3"))
    # The real (cohere) index is untouched.
    assert os.path.exists(paths.db_path(ws, "cohere"))


def test_all_workspaces_graph_skips_other_backend_without_fabricating(monkeypatch):
    """The cross-workspace graph views iterate list_indexed_workspaces() and open
    each db; a folder indexed under the other backend must be skipped rather than
    fabricated into an empty active-backend db."""
    from server.infrastructure import model_mode

    ws = "/fake/ws-graph-cohere-only"
    monkeypatch.setattr(model_mode, "resolve_backend", lambda cap, config=None: "cohere")
    store.set_meta(ws, workspace=ws)
    assert os.path.exists(paths.db_path(ws, "cohere"))

    monkeypatch.setattr(model_mode, "resolve_backend", lambda cap, config=None: "qwen3")
    g = graph_views.all_workspaces_graph()
    assert g["nodes"] == [] and g["workspaces"] == []
    gu = graph_views.all_workspaces_umap_graph()
    assert gu["nodes"] == []
    # Neither read fabricated the active-backend (qwen3) db.
    assert not os.path.exists(paths.db_path(ws, "qwen3"))
