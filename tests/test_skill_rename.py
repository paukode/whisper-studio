"""Renaming a markdown skill never deletes the file it just wrote and never
overwrites another skill."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.skills as skills_mod
import server.skills_routes  # noqa: F401  (registers the routes)


def _md(name: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: d\n---\n\n{body}\n"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    d = tmp_path / "skills"
    d.mkdir()
    monkeypatch.setattr(skills_mod, "SKILLS_DIR", str(d))
    monkeypatch.setattr(skills_mod, "SKILLS_CONFIG_PATH", str(tmp_path / "skills_config.json"))
    monkeypatch.setattr(skills_mod, "SKILLS", {})
    monkeypatch.setattr(skills_mod, "rebuild_tools", lambda: None)
    app = FastAPI()
    app.include_router(skills_mod.router)
    return TestClient(app), d


def test_a_rename_to_the_same_file_keeps_it(client):
    c, d = client
    (d / "foo-bar.md").write_text(_md("foo_bar", "old"))
    r = c.put("/api/skills/foo_bar", json={"content": _md("foo-bar", "new"), "new_name": "foo-bar"})
    assert r.status_code == 200, r.text
    assert "new" in (d / "foo-bar.md").read_text()


def test_a_rename_onto_another_skill_is_refused(client):
    c, d = client
    (d / "alpha.md").write_text(_md("alpha", "alpha body"))
    (d / "beta.md").write_text(_md("beta", "beta body"))
    r = c.put("/api/skills/alpha", json={"content": _md("beta", "x"), "new_name": "beta"})
    assert r.status_code == 409
    assert "beta body" in (d / "beta.md").read_text()
    assert (d / "alpha.md").exists()
