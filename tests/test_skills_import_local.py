"""Local-folder skill import (the "From local folder" UI path).

Covers the pure helpers (reconstruct/preview/import) and the multipart HTTP
routes. The local path reuses the git path's detection/validation/copy helpers,
so these tests focus on the tree-reconstruction safety (path escape, oversize)
and on the preview→import→hot-reload round trip that the dialog drives.
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
    """Point every skills-dir reference at a throwaway directory so imports land
    there and the loader (hot reload) reads the same tree."""
    d = tmp_path / "skills"
    d.mkdir()
    monkeypatch.setattr(si, "SKILLS_DIR", str(d))
    monkeypatch.setattr(skills_mod, "SKILLS_DIR", str(d))
    monkeypatch.setattr(skills_mod, "SKILLS_CONFIG_PATH", str(tmp_path / "skills_config.json"))
    return d


def _two_skill_tree() -> list[tuple[str, bytes]]:
    """A picked folder 'pack' holding two skill folders, one with a script."""
    return [
        ("pack/alpha/SKILL.md", _skill_md("alpha", "The alpha skill")),
        ("pack/alpha/scripts/run.py", b"print('hi')\n"),
        ("pack/beta/SKILL.md", _skill_md("beta", "The beta skill")),
        ("pack/README.md", b"# not a skill\n"),
    ]


# --- helpers -------------------------------------------------------------


def test_safe_rel_rejects_escapes_and_absolute():
    assert si._safe_rel("a/b/c") == "a/b/c"
    assert si._safe_rel("a\\b\\c") == "a/b/c"
    assert si._safe_rel("./a/./b") == "a/b"
    assert si._safe_rel("../x") is None
    assert si._safe_rel("a/../../x") is None
    assert si._safe_rel("/abs/x") is None
    assert si._safe_rel("C:/win") is None
    assert si._safe_rel("") is None


def test_preview_local_detects_folders():
    out = si.preview_local(_two_skill_tree())
    by_sub = {s["subpath"]: s for s in out["skills"]}
    assert set(by_sub) == {"pack/alpha", "pack/beta"}
    assert by_sub["pack/alpha"]["name"] == "alpha"
    assert by_sub["pack/alpha"]["description"] == "The alpha skill"
    assert by_sub["pack/alpha"]["hasScripts"] is True
    assert by_sub["pack/alpha"]["scriptFiles"] == ["run.py"]
    assert by_sub["pack/beta"]["hasScripts"] is False


def test_import_local_copies_and_hot_reloads(skills_dir):
    res = si.import_local(_two_skill_tree(), ["pack/alpha"], overwrite=False)
    assert [r["name"] for r in res["imported"]] == ["alpha"]
    assert res["conflicts"] == [] and res["errors"] == []
    # Copied under the basename, scripts included.
    assert (skills_dir / "alpha" / "SKILL.md").is_file()
    assert (skills_dir / "alpha" / "scripts" / "run.py").is_file()
    # Hot reload: the loader now sees it with no restart.
    loaded = skills_mod.load_skills(str(skills_dir))
    assert "alpha" in loaded and loaded["alpha"]["is_folder"] is True


def test_import_local_conflict_then_overwrite(skills_dir):
    tree = _two_skill_tree()
    assert si.import_local(tree, ["pack/alpha"])["imported"]
    # Second import without overwrite → conflict, not a clobber.
    again = si.import_local(tree, ["pack/alpha"], overwrite=False)
    assert again["imported"] == [] and len(again["conflicts"]) == 1
    # With overwrite it succeeds.
    forced = si.import_local(tree, ["pack/alpha"], overwrite=True)
    assert [r["name"] for r in forced["imported"]] == ["alpha"]


def test_import_local_rejects_path_escape(skills_dir):
    tree = [("../evil/SKILL.md", _skill_md("evil"))]
    with pytest.raises(si.SkillImportError):
        si.import_local(tree, ["../evil"])


def test_import_local_rejects_reserved_name(skills_dir):
    # Frontmatter name collides with a reserved built-in prefix (git_*).
    tree = [("pack/gitthing/SKILL.md", _skill_md("git_helper", "nope"))]
    res = si.import_local(tree, ["pack/gitthing"])
    assert res["imported"] == []
    assert res["errors"] and "reserved" in res["errors"][0]["reason"]


def test_import_local_rejects_oversize(skills_dir, monkeypatch):
    monkeypatch.setattr(si, "_MAX_FILE_BYTES", 64)
    tree = [("pack/big/SKILL.md", b"x" * 200)]
    with pytest.raises(si.SkillImportError):
        si.import_local(tree, ["pack/big"])


def test_import_local_rejects_unselected_subpath(skills_dir):
    # A client naming a folder that was not part of the upload is refused.
    res = si.import_local(_two_skill_tree(), ["pack/ghost"])
    assert res["imported"] == []
    assert res["errors"][0]["reason"] == "not in uploaded folder"


# --- HTTP routes (multipart) --------------------------------------------


@pytest.fixture()
def client(skills_dir):
    # Importing skills_routes wires the handlers onto the skills router.
    import server.skills_routes  # noqa: F401
    from server.skills import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _multipart(tree: list[tuple[str, bytes]]):
    return [("files", (rel, data, "application/octet-stream")) for rel, data in tree]


def test_route_local_preview_and_import_roundtrip(client, skills_dir):
    files = _multipart(_two_skill_tree())
    r = client.post("/api/skills/import/local/preview", files=files)
    assert r.status_code == 200
    subs = {s["subpath"] for s in r.json()["skills"]}
    assert subs == {"pack/alpha", "pack/beta"}

    r2 = client.post(
        "/api/skills/import/local",
        files=_multipart(_two_skill_tree()),
        data={"subpaths": json.dumps(["pack/alpha", "pack/beta"]), "overwrite": "false"},
    )
    assert r2.status_code == 200
    assert {x["name"] for x in r2.json()["imported"]} == {"alpha", "beta"}

    # Hot-reloaded: the skills endpoint now lists them with no restart.
    listed = {s["name"] for s in client.get("/api/skills").json()["skills"]}
    assert {"alpha", "beta"} <= listed


def test_route_local_import_bad_subpaths_json(client):
    r = client.post(
        "/api/skills/import/local",
        files=_multipart(_two_skill_tree()),
        data={"subpaths": "not-json", "overwrite": "false"},
    )
    assert r.status_code == 400
