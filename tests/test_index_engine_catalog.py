"""The index engine catalog is filtered by the active model mode.

Local mode must never offer a cloud engine (nothing may call Bedrock) nor a
model whose weights are not on disk (a folder setting would otherwise kick off a
multi-gigabyte download); cloud and hybrid keep Haiku plus the whole on-device
catalog with its download badges.
"""

from server.index import engines
from server.local import registry as local_registry
from server.local import runtime as local_rt

_MODELS = {
    "local_gemma": {"label": "Gemma 4 12B (Local)"},
    "local_qwen35_9b": {"label": "Qwen3.5 9B (Local)"},
}


def _catalog(monkeypatch, mode, models=None, downloaded=()):
    monkeypatch.setattr(
        local_registry, "local_models", lambda: models if models is not None else _MODELS
    )
    monkeypatch.setattr(local_rt, "is_downloaded", lambda key: key in downloaded)
    return engines.engine_catalog(mode)


def test_cloud_mode_lists_haiku_and_every_local_model(monkeypatch):
    rows = _catalog(monkeypatch, "cloud", downloaded=("local_qwen35_9b",))
    assert [r["key"] for r in rows] == ["haiku", "local_gemma", "local_qwen35_9b"]
    # Download state rides along so the picker can badge a first-pick download.
    assert {r["key"]: r["downloaded"] for r in rows if r["local"]} == {
        "local_gemma": False,
        "local_qwen35_9b": True,
    }


def test_hybrid_mode_matches_cloud(monkeypatch):
    rows = _catalog(monkeypatch, "hybrid", downloaded=("local_gemma",))
    assert [r["key"] for r in rows] == ["haiku", "local_gemma", "local_qwen35_9b"]


def test_local_mode_drops_haiku_and_undownloaded_models(monkeypatch):
    rows = _catalog(monkeypatch, "local", downloaded=("local_qwen35_9b",))
    assert [r["key"] for r in rows] == ["local_qwen35_9b"]
    assert all(r["local"] and r["downloaded"] for r in rows)


def test_local_mode_with_nothing_installed_is_empty(monkeypatch):
    assert _catalog(monkeypatch, "local") == []
    assert _catalog(monkeypatch, "local", models={}) == []


def test_mode_defaults_to_the_configured_model_mode(monkeypatch):
    from server.infrastructure import model_mode as mm

    monkeypatch.setattr(mm, "current_mode", lambda config=None: "local")
    rows = _catalog(monkeypatch, None, downloaded=("local_gemma",))
    assert [r["key"] for r in rows] == ["local_gemma"]


def test_endpoint_ships_the_mode_with_the_rows(monkeypatch):
    """The picker needs the mode to explain an empty list, so it travels with
    the catalog rather than being read from a second endpoint."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.index import router as index_router
    from server.infrastructure import model_mode as mm

    monkeypatch.setattr(mm, "current_mode", lambda config=None: "local")
    monkeypatch.setattr(local_registry, "local_models", lambda: _MODELS)
    monkeypatch.setattr(local_rt, "is_downloaded", lambda key: key == "local_qwen35_9b")

    app = FastAPI()
    app.include_router(index_router)
    body = TestClient(app).get("/api/workspace/index/engines").json()
    assert body["mode"] == "local"
    assert [r["key"] for r in body["engines"]] == ["local_qwen35_9b"]
