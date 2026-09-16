"""server.agent_tools.skill_manage: the assistant creates and maintains folder
skills under the same guards the HTTP routes apply."""

from __future__ import annotations

import json
import os

import pytest

import server.skills as _sk
from server.agent_tools import skill_manage as sm


@pytest.fixture(autouse=True)
def _skills_dir(tmp_path, monkeypatch):
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(_sk, "SKILLS_DIR", str(skills))
    monkeypatch.setattr(_sk, "SKILLS_CONFIG_PATH", str(tmp_path / "skills_config.json"))
    monkeypatch.setattr(_sk, "SKILLS", {})
    monkeypatch.setattr(_sk, "TOOLS", [])
    monkeypatch.setattr(sm, "_enabled", lambda: True)
    yield skills


def _call(**kw):
    return json.loads(sm.execute_skill_manage(kw, "s1"))


def test_create_view_patch_append_and_files(_skills_dir):
    out = _call(
        action="create",
        name="deploy_staging",
        description="Deploy the app to staging the way this team does it.",
        content="# Deploy staging\n\n1. Run the build.\n2. Push the image.",
        triggers=["deploy staging", "ship to staging"],
    )
    assert out["ok"] and out["action"] == "create"
    md = _skills_dir / "deploy_staging" / "SKILL.md"
    text = md.read_text()
    assert "created_by: agent" in text and "triggers: deploy staging, ship to staging" in text
    assert "deploy_staging" in _sk.SKILLS  # reloaded into the live catalog

    view = _call(action="view", name="deploy_staging")
    assert view["kind"] == "folder" and view["created_by_agent"] is True
    assert "Push the image" in view["content"]

    dup = _call(action="create", name="deploy_staging", description="x", content="y")
    assert "already exists" in dup["error"]

    patched = _call(
        action="patch",
        name="deploy_staging",
        old_text="Push the image.",
        new_text="Push the image, then wait for the health check.",
    )
    assert patched["ok"] and "health check" in md.read_text()
    missing = _call(action="patch", name="deploy_staging", old_text="not here", new_text="x")
    assert "not found" in missing["error"]
    ambiguous = _call(action="patch", name="deploy_staging", old_text="the", new_text="x")
    assert "matches" in ambiguous["error"]

    appended = _call(
        action="append", name="deploy_staging", content="## Pitfalls\n- Never deploy on Friday."
    )
    assert appended["ok"] and md.read_text().rstrip().endswith("Never deploy on Friday.")

    wf = _call(
        action="write_file",
        name="deploy_staging",
        path="references/rollback.md",
        content="Rollback steps",
    )
    assert (
        wf["ok"]
        and (_skills_dir / "deploy_staging" / "references" / "rollback.md").read_text()
        == "Rollback steps"
    )
    assert {f["path"] for f in _call(action="view", name="deploy_staging")["files"]} == {
        "SKILL.md",
        "references/rollback.md",
    }
    rm = _call(action="remove_file", name="deploy_staging", path="references/rollback.md")
    assert rm["ok"] and not (_skills_dir / "deploy_staging" / "references" / "rollback.md").exists()


def test_name_and_path_guards(_skills_dir):
    assert (
        "name must be"
        in _call(action="create", name="Bad Name", description="d", content="c")["error"]
    )
    assert (
        "collides"
        in _call(action="create", name="git_status", description="d", content="c")["error"]
    )
    assert "name is required" in _call(action="view", name="")["error"]
    _call(action="create", name="notes", description="d", content="c")
    for bad in ("../escape.md", "references/../../x.md", "SKILL.md", "other/x.md", "references"):
        out = _call(action="write_file", name="notes", path=bad, content="x")
        assert "error" in out, bad
    assert not (_skills_dir.parent / "escape.md").exists()
    assert "action must be" in _call(action="explode", name="notes")["error"]


def test_delete_only_for_agent_created_skills(_skills_dir):
    _call(action="create", name="mine", description="d", content="c")
    theirs = _skills_dir / "theirs"
    theirs.mkdir()
    (theirs / "SKILL.md").write_text("---\nname: theirs\ndescription: user made\n---\nbody\n")
    _sk.SKILLS = _sk.load_skills()
    assert _call(action="delete", name="theirs")["error"].startswith(
        "only skills the assistant created"
    )
    assert (theirs / "SKILL.md").exists()
    assert _call(action="delete", name="mine")["ok"]
    assert not (_skills_dir / "mine").exists() and "mine" not in _sk.SKILLS


def test_flat_skill_can_be_viewed_and_patched_but_not_deleted(_skills_dir):
    flat = _skills_dir / "quick-note.md"
    flat.write_text("---\nname: quick_note\ndescription: flat skill\n---\nDo the thing.\n")
    _sk.SKILLS = _sk.load_skills()
    view = _call(action="view", name="quick_note")
    assert view["kind"] == "file" and "Do the thing" in view["content"]
    assert _call(
        action="patch", name="quick_note", old_text="Do the thing.", new_text="Do it well."
    )["ok"]
    assert "Do it well." in flat.read_text()
    assert "Settings" in _call(action="delete", name="quick_note")["error"]


def test_disabled_flag_refuses(monkeypatch):
    monkeypatch.setattr(sm, "_enabled", lambda: False)
    assert "disabled" in _call(action="view", name="x")["error"]


def test_no_traversal_in_create_name(_skills_dir):
    out = _call(action="create", name="a..b", description="d", content="c")
    assert "error" in out
    assert not os.path.exists(os.path.join(str(_skills_dir), "a..b"))
