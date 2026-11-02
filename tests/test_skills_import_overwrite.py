"""Skill import with "Overwrite existing" ticked (server/skills_import.py).

Unticked, a taken name is a conflict. Ticked, the import replaces the
installed skill that holds the name: the folder skill of that name, or a flat
.md skill whose loaded name matches. The old version waits outside the skills
folder until the copy succeeds and comes back when it fails. A skill that
came with the app is replaced like any other; a reserved built-in tool name
is never taken.
Saving, editing or renaming a skill still never takes another skill's name.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.skills as skills_mod
import server.skills_import as si


def _skill_md(name: str, description: str = "") -> bytes:
    return (
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nDo the thing.\n"
    ).encode()


@pytest.fixture()
def skills_dir(tmp_path, monkeypatch):
    d = tmp_path / "skills"
    d.mkdir()
    monkeypatch.setattr(si, "SKILLS_DIR", str(d))
    monkeypatch.setattr(skills_mod, "SKILLS_DIR", str(d))
    monkeypatch.setattr(skills_mod, "SKILLS_CONFIG_PATH", str(tmp_path / "skills_config.json"))
    monkeypatch.setattr(skills_mod, "SKILLS", {})
    return d


def _reload(monkeypatch, skills_dir):
    monkeypatch.setattr(skills_mod, "SKILLS", skills_mod.load_skills(str(skills_dir)))


def _install_alpha(monkeypatch, skills_dir, description="old version"):
    old = [("pack/alpha/SKILL.md", _skill_md("alpha", description))]
    assert si.import_local(old, ["pack/alpha"])["imported"]
    _reload(monkeypatch, skills_dir)


NEW_ALPHA = [
    ("pack/alpha/SKILL.md", _skill_md("alpha", "new version")),
    ("pack/alpha/scripts/run.py", b"print('new')\n"),
]


def test_unticked_a_taken_name_is_a_conflict(skills_dir, monkeypatch):
    _install_alpha(monkeypatch, skills_dir)

    res = si.import_local(NEW_ALPHA, ["pack/alpha"], overwrite=False)

    assert res["imported"] == []
    assert res["conflicts"][0]["reason"] == "a skill named alpha already exists"
    assert "old version" in (skills_dir / "alpha" / "SKILL.md").read_text()


def test_ticked_the_folder_skill_of_that_name_is_replaced(skills_dir, monkeypatch):
    _install_alpha(monkeypatch, skills_dir)

    res = si.import_local(NEW_ALPHA, ["pack/alpha"], overwrite=True)

    assert [r["name"] for r in res["imported"]] == ["alpha"]
    assert res["imported"][0]["replaced"] is True
    # No backup is left in the skills folder, where it would load as a skill.
    assert sorted(p.name for p in skills_dir.iterdir()) == ["alpha"]
    loaded = skills_mod.load_skills(str(skills_dir))
    assert loaded["alpha"]["description"] == "new version"
    assert (skills_dir / "alpha" / "scripts" / "run.py").is_file()


def test_ticked_a_flat_skill_with_the_loaded_name_is_replaced(skills_dir, monkeypatch):
    # The file name differs; the frontmatter name decides.
    (skills_dir / "my-alpha-notes.md").write_bytes(_skill_md("alpha", "flat version"))
    (skills_dir / "other.md").write_bytes(_skill_md("other", "keep me"))
    _reload(monkeypatch, skills_dir)

    res = si.import_local(NEW_ALPHA, ["pack/alpha"], overwrite=True)

    assert [r["name"] for r in res["imported"]] == ["alpha"]
    assert not (skills_dir / "my-alpha-notes.md").exists()
    assert (skills_dir / "other.md").exists()
    loaded = skills_mod.load_skills(str(skills_dir))
    assert loaded["alpha"]["description"] == "new version"
    assert loaded["other"]["description"] == "keep me"


def test_ticked_a_folder_with_another_name_replaces_the_skill_of_that_name(skills_dir, monkeypatch):
    _install_alpha(monkeypatch, skills_dir)

    res = si.import_local(
        [("pack/alpha-v2/SKILL.md", _skill_md("alpha", "v2"))], ["pack/alpha-v2"], overwrite=True
    )

    assert res["imported"] and not res["conflicts"]
    assert sorted(p.name for p in skills_dir.iterdir()) == ["alpha-v2"]
    assert skills_mod.load_skills(str(skills_dir))["alpha"]["description"] == "v2"


def test_ticked_a_failed_copy_restores_the_old_version(skills_dir, monkeypatch):
    _install_alpha(monkeypatch, skills_dir)
    (skills_dir / "alpha.md").write_bytes(_skill_md("alpha", "flat twin"))

    def broken(src, dest):
        import os

        os.makedirs(dest)
        raise si.SkillImportError("copy failed")

    monkeypatch.setattr(si, "_copy_skill_folder", broken)
    res = si.import_local(NEW_ALPHA, ["pack/alpha"], overwrite=True)

    assert res["errors"] and res["errors"][0]["reason"] == "copy failed"
    assert sorted(p.name for p in skills_dir.iterdir()) == ["alpha", "alpha.md"]
    assert "old version" in (skills_dir / "alpha" / "SKILL.md").read_text()
    assert "flat twin" in (skills_dir / "alpha.md").read_text()


def _trust(*names):
    config = skills_mod.load_skills_config()
    config["trusted"] = list(names)
    skills_mod.save_skills_config(config)


def test_ticked_a_replaced_trusted_skill_is_no_longer_trusted(skills_dir, monkeypatch):
    _install_alpha(monkeypatch, skills_dir)
    _trust("alpha", "other")
    script = skills_dir / "alpha" / "scripts" / "run.py"

    res = si.import_local(NEW_ALPHA, ["pack/alpha"], overwrite=True)
    _reload(monkeypatch, skills_dir)

    assert res["imported"] and res["imported"][0]["replaced"] is True
    assert skills_mod.load_skills_config()["trusted"] == ["other"]
    assert script.is_file()
    assert skills_mod.command_runs_trusted_skill(f"python3 {script}") is False


def test_ticked_a_failed_copy_keeps_the_trust(skills_dir, monkeypatch):
    _install_alpha(monkeypatch, skills_dir)
    _trust("alpha")

    def broken(src, dest):
        raise si.SkillImportError("copy failed")

    monkeypatch.setattr(si, "_copy_skill_folder", broken)
    res = si.import_local(NEW_ALPHA, ["pack/alpha"], overwrite=True)

    assert res["errors"]
    assert skills_mod.load_skills_config()["trusted"] == ["alpha"]


def test_ticked_a_skill_that_came_with_the_app_is_replaced(skills_dir, monkeypatch):
    # The seeded copy belongs to the user, who can already edit or delete it.
    # In a source install the skills folder IS the repo skills/ folder, so a
    # "shipped" check there matched every skill and the tick never worked.
    (skills_dir / "meeting-notes.md").write_bytes(_skill_md("meeting_notes", "shipped"))
    _reload(monkeypatch, skills_dir)

    res = si.import_local(
        [("pack/meeting-notes/SKILL.md", _skill_md("meeting_notes", "imported"))],
        ["pack/meeting-notes"],
        overwrite=True,
    )

    assert res["conflicts"] == []
    assert [s["name"] for s in res["imported"]] == ["meeting_notes"]
    assert not (skills_dir / "meeting-notes.md").exists()
    assert "imported" in (skills_dir / "meeting-notes" / "SKILL.md").read_text()


def test_ticked_a_reserved_built_in_name_is_still_refused(skills_dir):
    res = si.import_local(
        [("pack/tr/SKILL.md", _skill_md("terminal_run"))], ["pack/tr"], overwrite=True
    )
    assert res["imported"] == []
    assert "reserved" in res["errors"][0]["reason"]


def test_ticked_a_folder_that_holds_another_skill_is_not_replaced(skills_dir, monkeypatch):
    # The folder 'alpha' holds the skill 'beta'; importing 'alpha' into it
    # would delete beta.
    (skills_dir / "alpha").mkdir()
    (skills_dir / "alpha" / "SKILL.md").write_bytes(_skill_md("beta", "keep me"))
    _reload(monkeypatch, skills_dir)

    res = si.import_local(NEW_ALPHA, ["pack/alpha"], overwrite=True)

    assert res["imported"] == []
    assert "already holds something other than alpha" in res["conflicts"][0]["reason"]
    assert "keep me" in (skills_dir / "alpha" / "SKILL.md").read_text()


def test_ticked_a_taken_name_with_nothing_in_the_skills_folder_is_a_conflict(
    skills_dir, monkeypatch
):
    monkeypatch.setattr(skills_mod, "SKILLS", {"alpha": {"name": "alpha"}})

    res = si.import_local(NEW_ALPHA, ["pack/alpha"], overwrite=True)

    assert res["imported"] == []
    assert "outside the skills folder" in res["conflicts"][0]["reason"]
    assert list(skills_dir.iterdir()) == []


def test_ticked_a_new_name_imports_as_usual(skills_dir):
    res = si.import_local(NEW_ALPHA, ["pack/alpha"], overwrite=True)
    assert res["imported"][0]["replaced"] is False


def test_two_selected_folders_with_one_name_still_import_once(skills_dir):
    tree = [
        ("pack/one/SKILL.md", _skill_md("same", "first")),
        ("pack/two/SKILL.md", _skill_md("same", "second")),
    ]
    res = si.import_local(tree, ["pack/one", "pack/two"], overwrite=True)
    assert [r["subpath"] for r in res["imported"]] == ["pack/one"]
    assert [r["subpath"] for r in res["conflicts"]] == ["pack/two"]


# --- HTTP routes ---------------------------------------------------------


@pytest.fixture()
def client(skills_dir):
    import server.skills_routes  # noqa: F401
    from server.skills import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _multipart(tree):
    return [("files", (rel, data, "application/octet-stream")) for rel, data in tree]


def test_route_local_overwrite_field(client, skills_dir, monkeypatch):
    _install_alpha(monkeypatch, skills_dir)

    plain = client.post(
        "/api/skills/import/local",
        files=_multipart(NEW_ALPHA),
        data={"subpaths": json.dumps(["pack/alpha"])},
    )
    assert plain.status_code == 200 and len(plain.json()["conflicts"]) == 1

    forced = client.post(
        "/api/skills/import/local",
        files=_multipart(NEW_ALPHA),
        data={"subpaths": json.dumps(["pack/alpha"]), "overwrite": "true"},
    )
    assert forced.status_code == 200, forced.text
    assert [r["name"] for r in forced.json()["imported"]] == ["alpha"]
    assert skills_mod.SKILLS["alpha"]["description"] == "new version"


def test_route_local_overwrite_must_be_true_or_false(client):
    r = client.post(
        "/api/skills/import/local",
        files=_multipart(NEW_ALPHA),
        data={"subpaths": json.dumps(["pack/alpha"]), "overwrite": "maybe"},
    )
    assert r.status_code == 400


def test_route_git_passes_the_overwrite_flag(client, monkeypatch):
    seen = {}

    def fake_import(url, subpaths, overwrite=False):
        seen.update(url=url, subpaths=subpaths, overwrite=overwrite)
        return {"imported": [], "conflicts": [], "errors": []}

    monkeypatch.setattr(si, "import_skills", fake_import)
    r = client.post(
        "/api/skills/import",
        json={"url": "https://github.com/o/r", "subpaths": ["a"], "overwrite": True},
    )
    assert r.status_code == 200
    assert seen["overwrite"] is True
    r = client.post("/api/skills/import", json={"url": "https://github.com/o/r", "subpaths": ["a"]})
    assert r.status_code == 200 and seen["overwrite"] is False
    bad = client.post(
        "/api/skills/import",
        json={"url": "https://github.com/o/r", "subpaths": ["a"], "overwrite": "yes"},
    )
    assert bad.status_code == 400
