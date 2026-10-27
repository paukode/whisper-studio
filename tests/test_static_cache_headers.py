"""Cache headers on the frontend files.

The Mac app keeps one localhost origin across launches, so WebKit's cache
outlives an update. Only content-hashed bundles may be cached for good; the
page and every file that keeps its name across builds must be revalidated,
or an updated app shows the previous version's UI.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.main as main


def _client(tmp_path):
    (tmp_path / "dist" / "assets").mkdir(parents=True)
    (tmp_path / "dist" / "assets" / "index-abc123.js").write_text("x")
    (tmp_path / "dist" / "index.html").write_text("<!doctype html>")
    (tmp_path / "modules").mkdir()
    (tmp_path / "modules" / "themes.css").write_text(":root{}")
    app = FastAPI()
    app.mount("/static", main._CachedStatic(directory=str(tmp_path)), name="static")
    return TestClient(app)


def test_hashed_bundles_are_immutable(tmp_path):
    resp = _client(tmp_path).get("/static/dist/assets/index-abc123.js")
    assert resp.status_code == 200
    assert "immutable" in resp.headers["cache-control"]


def test_named_static_files_are_revalidated(tmp_path):
    resp = _client(tmp_path).get("/static/modules/themes.css")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"


def test_the_page_is_revalidated(tmp_path, monkeypatch):
    (tmp_path / "static" / "dist").mkdir(parents=True)
    (tmp_path / "static" / "dist" / "index.html").write_text("<!doctype html>")
    monkeypatch.setattr(main, "BASE_DIR", str(tmp_path))
    resp = main._spa_index_response()
    assert resp.headers["cache-control"] == "no-cache"
