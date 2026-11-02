"""Skill edits through the routes: a rename never deletes the file it just
wrote; no save, edit or rename takes another skill's name; a delete that fails
says why."""

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


def test_a_folder_skill_that_cannot_be_deleted_says_why(client, monkeypatch):
    import shutil

    c, d = client
    folder = d / "alpha"
    folder.mkdir()
    (folder / "SKILL.md").write_text(_md("alpha", "body"))
    monkeypatch.setattr(skills_mod, "SKILLS", skills_mod.load_skills(str(d)))

    def refuse(path, *a, **k):
        raise PermissionError(13, "Operation not permitted", str(path))

    monkeypatch.setattr(shutil, "rmtree", refuse)
    r = c.delete("/api/skills/alpha")
    assert r.status_code == 500
    assert "Could not delete" in r.json()["error"]


def _load(monkeypatch, d):
    monkeypatch.setattr(skills_mod, "SKILLS", skills_mod.load_skills(str(d)))


def test_a_new_skill_with_a_taken_name_is_refused(client, monkeypatch):
    """Case and '-' versus '_' do not make a different name."""
    c, d = client
    (d / "alpha-beta.md").write_text(_md("alpha_beta", "kept"))
    _load(monkeypatch, d)
    r = c.post("/api/skills", json={"name": "Alpha-Beta", "content": _md("Alpha-Beta", "x")})
    assert r.status_code == 409
    assert r.json()["error"] == "A skill named alpha_beta already exists"
    assert sorted(p.name for p in d.iterdir()) == ["alpha-beta.md"]


def test_a_new_skill_whose_frontmatter_names_another_skill_is_refused(client, monkeypatch):
    """The skill loads under its frontmatter name, whatever the file is called."""
    c, d = client
    (d / "alpha.md").write_text(_md("alpha", "kept"))
    _load(monkeypatch, d)
    r = c.post("/api/skills", json={"name": "gamma", "content": _md("alpha", "x")})
    assert r.status_code == 409
    assert not (d / "gamma.md").exists()


def test_a_new_skill_named_like_a_folder_skill_is_refused(client, monkeypatch):
    c, d = client
    (d / "alpha").mkdir()
    (d / "alpha" / "SKILL.md").write_text(_md("alpha", "folder"))
    _load(monkeypatch, d)
    r = c.post("/api/skills", json={"name": "alpha", "content": _md("alpha", "x")})
    assert r.status_code == 409
    assert not (d / "alpha.md").exists()


def test_an_edit_that_renames_in_the_frontmatter_onto_another_skill_is_refused(client, monkeypatch):
    c, d = client
    (d / "alpha.md").write_text(_md("alpha", "alpha body"))
    (d / "beta.md").write_text(_md("beta", "beta body"))
    _load(monkeypatch, d)
    r = c.put("/api/skills/alpha", json={"content": _md("beta", "changed")})
    assert r.status_code == 409
    assert "alpha body" in (d / "alpha.md").read_text()


def test_an_edit_that_keeps_its_own_name_saves(client, monkeypatch):
    c, d = client
    (d / "alpha.md").write_text(_md("alpha", "old"))
    _load(monkeypatch, d)
    r = c.put("/api/skills/alpha", json={"content": _md("alpha", "new")})
    assert r.status_code == 200, r.text
    assert "new" in (d / "alpha.md").read_text()


def test_a_rename_onto_a_folder_skill_is_refused(client, monkeypatch):
    c, d = client
    (d / "alpha.md").write_text(_md("alpha", "alpha body"))
    (d / "beta").mkdir()
    (d / "beta" / "SKILL.md").write_text(_md("beta", "folder"))
    _load(monkeypatch, d)
    r = c.put("/api/skills/alpha", json={"content": _md("beta", "x"), "new_name": "beta"})
    assert r.status_code == 409
    assert r.json()["error"] == "A skill named beta already exists"
    assert (d / "alpha.md").exists() and not (d / "beta.md").exists()
