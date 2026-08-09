"""Import an existing Claude Code or Codex CLI project into whisper's native
WHISPER.md / folder-skills / MCP formats.

Covers the pure translation functions (server.onboarding.import_external) and
the HTTP route. Skills are copied through the same validated path
server/skills_import.py uses for its own import feature, so these tests focus
on: WHISPER.md write-vs-append, skills actually being loadable by the real
folder_skills loader afterward, MCP servers merging without clobbering
existing ones, and the route's error handling.
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.mcp as mcp_module
import server.skills as skills_mod
import server.skills_import as si
from server.onboarding import import_external as ie

# --- fixtures --------------------------------------------------------------


@pytest.fixture()
def isolated_skills(tmp_path, monkeypatch):
    """Point every skills-dir reference at a throwaway directory, mirroring
    tests/test_skills_import_local.py, so imports land there and the real
    loader reads the same tree."""
    d = tmp_path / "skills"
    d.mkdir()
    monkeypatch.setattr(si, "SKILLS_DIR", str(d))
    monkeypatch.setattr(skills_mod, "SKILLS_DIR", str(d))
    monkeypatch.setattr(skills_mod, "SKILLS_CONFIG_PATH", str(tmp_path / "skills_config.json"))
    return d


@pytest.fixture()
def isolated_mcp(tmp_path, monkeypatch):
    """Point MCP_CONFIG_PATH at a throwaway file, mirroring
    tests/test_mcp_toggle_endpoint.py, so the merge writes there instead of
    the user's real mcp_servers.json."""
    path = str(tmp_path / "mcp_servers.json")
    monkeypatch.setattr(mcp_module, "MCP_CONFIG_PATH", path)
    return path


def _skill_md(name: str, description: str = "") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nDo the thing.\n"


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _make_claude_project(base: str, *, mcp_server_name: str = "filesystem") -> str:
    """A fake Claude Code project: CLAUDE.md, .claude/skills/alpha, .mcp.json."""
    _write(
        os.path.join(base, "CLAUDE.md"), "# Project rules\n\nAlways run tests before committing.\n"
    )
    _write(
        os.path.join(base, ".claude/skills/alpha/SKILL.md"), _skill_md("alpha", "The alpha skill")
    )
    _write(os.path.join(base, ".claude/skills/alpha/scripts/run.py"), "print('hi')\n")
    _write(
        os.path.join(base, ".mcp.json"),
        json.dumps(
            {
                "mcpServers": {
                    mcp_server_name: {
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                        "env": {},
                    }
                }
            }
        ),
    )
    return base


def _make_codex_project(base: str) -> str:
    """A fake Codex CLI project: AGENTS.md, .codex/skills/beta, .codex/config.toml."""
    _write(os.path.join(base, "AGENTS.md"), "# Agent notes\n\nPrefer small diffs.\n")
    _write(os.path.join(base, ".codex/skills/beta/SKILL.md"), _skill_md("beta", "The beta skill"))
    _write(
        os.path.join(base, ".codex/config.toml"),
        "[mcp_servers.foo]\n"
        'command = "npx"\n'
        'args = ["-y", "@some/server"]\n'
        'env = { API_KEY = "abc" }\n'
        "\n"
        "[some_other_table]\n"
        'unrelated = "value"\n',
    )
    return base


# --- instructions: CLAUDE.md / AGENTS.md -> WHISPER.md ----------------------


def test_claude_import_writes_whisper_md(tmp_path, isolated_skills, isolated_mcp):
    src = _make_claude_project(str(tmp_path / "src"))
    ws = tmp_path / "ws"
    ws.mkdir()

    result = ie.import_claude_project(src, str(ws))

    assert result["instructions"]["status"] == "written"
    dest = ws / "WHISPER.md"
    assert dest.is_file()
    content = dest.read_text()
    assert "Migrated from Claude Code" in content
    assert "Always run tests before committing." in content


def test_claude_import_appends_to_existing_whisper_md(tmp_path, isolated_skills, isolated_mcp):
    src = _make_claude_project(str(tmp_path / "src"))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "WHISPER.md").write_text("Existing project rules.\n")

    result = ie.import_claude_project(src, str(ws))

    assert result["instructions"]["status"] == "appended"
    content = (ws / "WHISPER.md").read_text()
    assert "Existing project rules." in content
    assert "Always run tests before committing." in content
    assert "Imported from Claude Code" in content


def test_missing_instructions_file_is_skipped_not_fatal(tmp_path, isolated_skills, isolated_mcp):
    src = tmp_path / "src"
    src.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()

    result = ie.import_claude_project(str(src), str(ws))

    assert result["instructions"]["status"] == "skipped"
    assert "CLAUDE.md" in result["instructions"]["reason"]
    assert not (ws / "WHISPER.md").exists()


# --- skills: copied and actually loadable by the real loader ---------------


def test_claude_skills_are_copied_and_loadable(tmp_path, isolated_skills, isolated_mcp):
    src = _make_claude_project(str(tmp_path / "src"))
    ws = tmp_path / "ws"
    ws.mkdir()

    result = ie.import_claude_project(src, str(ws))

    assert [s["name"] for s in result["skills"]["copied"]] == ["alpha"]
    assert result["skills"]["skipped"] == []
    assert (isolated_skills / "alpha" / "SKILL.md").is_file()
    assert (isolated_skills / "alpha" / "scripts" / "run.py").is_file()

    # Prove the shape is right by loading it through the REAL loader, not just
    # checking the copy landed on disk.
    loaded = skills_mod.load_skills(str(isolated_skills))
    assert "alpha" in loaded
    assert loaded["alpha"]["is_folder"] is True
    assert loaded["alpha"]["description"] == "The alpha skill"
    assert loaded["alpha"]["has_scripts"] is True


def test_codex_skills_are_copied_and_loadable(tmp_path, isolated_skills, isolated_mcp):
    src = _make_codex_project(str(tmp_path / "src"))
    ws = tmp_path / "ws"
    ws.mkdir()

    result = ie.import_codex_project(src, str(ws))

    assert [s["name"] for s in result["skills"]["copied"]] == ["beta"]
    loaded = skills_mod.load_skills(str(isolated_skills))
    assert "beta" in loaded and loaded["beta"]["is_folder"] is True


def test_skill_conflict_is_skipped_not_overwritten(tmp_path, isolated_skills, isolated_mcp):
    src = _make_claude_project(str(tmp_path / "src"))
    ws = tmp_path / "ws"
    ws.mkdir()

    first = ie.import_claude_project(src, str(ws))
    assert [s["name"] for s in first["skills"]["copied"]] == ["alpha"]

    # Re-importing the identical source is a no-op skip, not a re-copy.
    second = ie.import_claude_project(src, str(ws))
    assert second["skills"]["copied"] == []
    assert second["skills"]["skipped"][0]["name"] == "alpha"
    assert "identical" in second["skills"]["skipped"][0]["reason"]

    # Now change the destination's content behind whisper's back and
    # re-import: the differing-content reason is reported, and the existing
    # (edited) file is left alone rather than being clobbered.
    dest_md = isolated_skills / "alpha" / "SKILL.md"
    dest_md.write_text(dest_md.read_text() + "\nEdited locally.\n")
    third = ie.import_claude_project(src, str(ws))
    assert third["skills"]["copied"] == []
    reason = third["skills"]["skipped"][0]["reason"]
    assert "different content" in reason
    assert "Edited locally." in dest_md.read_text()


def test_skill_with_reserved_name_is_skipped(tmp_path, isolated_skills, isolated_mcp):
    src = tmp_path / "src"
    _write(str(src / "CLAUDE.md"), "rules")
    _write(str(src / ".claude/skills/gitthing/SKILL.md"), _skill_md("git_helper", "nope"))
    ws = tmp_path / "ws"
    ws.mkdir()

    result = ie.import_claude_project(str(src), str(ws))

    assert result["skills"]["copied"] == []
    assert "reserved" in result["skills"]["skipped"][0]["reason"]


# --- MCP servers: merge, never clobber -------------------------------------


def test_mcp_servers_are_merged_not_overwritten(tmp_path, isolated_skills, isolated_mcp):
    # An existing server already configured, untouched by the import.
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "existing_srv": {
                        "command": "already-there",
                        "args": [],
                        "env": {},
                        "enabled": True,
                    }
                }
            },
            f,
        )
    src = _make_claude_project(str(tmp_path / "src"))
    ws = tmp_path / "ws"
    ws.mkdir()

    result = ie.import_claude_project(src, str(ws))

    assert result["mcp"]["added"] == ["filesystem"]
    assert result["mcp"]["skipped"] == []
    with open(isolated_mcp) as f:
        servers = json.load(f)["servers"]
    assert servers["existing_srv"]["command"] == "already-there"  # untouched
    assert servers["filesystem"]["command"] == "npx"
    assert servers["filesystem"]["enabled"] is True


def test_mcp_server_name_collision_is_skipped_not_overwritten(
    tmp_path, isolated_skills, isolated_mcp
):
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "filesystem": {
                        "command": "my-own-version",
                        "args": ["--custom"],
                        "env": {},
                        "enabled": False,
                    }
                }
            },
            f,
        )
    src = _make_claude_project(str(tmp_path / "src"))
    ws = tmp_path / "ws"
    ws.mkdir()

    result = ie.import_claude_project(src, str(ws))

    assert result["mcp"]["added"] == []
    assert result["mcp"]["skipped"] == [{"name": "filesystem", "reason": "already configured"}]
    with open(isolated_mcp) as f:
        servers = json.load(f)["servers"]
    assert servers["filesystem"]["command"] == "my-own-version"  # not clobbered


def test_no_mcp_config_found_is_reported_not_fatal(tmp_path, isolated_skills, isolated_mcp):
    src = tmp_path / "src"
    _write(str(src / "CLAUDE.md"), "rules")
    ws = tmp_path / "ws"
    ws.mkdir()

    result = ie.import_claude_project(str(src), str(ws))

    assert result["mcp"]["added"] == []
    assert result["mcp"]["source"] is None
    assert "no MCP server config" in result["mcp"]["reason"]


def test_codex_mcp_servers_parsed_from_config_toml(tmp_path, isolated_skills, isolated_mcp):
    src = _make_codex_project(str(tmp_path / "src"))
    ws = tmp_path / "ws"
    ws.mkdir()

    result = ie.import_codex_project(src, str(ws))

    assert result["mcp"]["added"] == ["foo"]
    with open(isolated_mcp) as f:
        servers = json.load(f)["servers"]
    assert servers["foo"]["command"] == "npx"
    assert servers["foo"]["args"] == ["-y", "@some/server"]
    assert servers["foo"]["env"] == {"API_KEY": "abc"}


# --- the minimal TOML fallback parser (exercised directly; tomllib covers
# --- the primary path on this test runner's Python 3.11+) ------------------


def test_toml_fallback_parses_mcp_servers_and_ignores_other_tables():
    text = (
        "[mcp_servers.alpha]\n"
        'command = "npx"\n'
        'args = ["-y", "server-a"]\n'
        'env = { KEY = "value", OTHER = "x" }\n'
        "\n"
        "[mcp_servers.beta]\n"
        'command = "/usr/bin/beta"\n'
        "\n"
        "[unrelated]\n"
        'foo = "bar"\n'
    )
    out = ie.parse_mcp_servers_toml_fallback(text)
    assert set(out) == {"alpha", "beta"}
    assert out["alpha"]["command"] == "npx"
    assert out["alpha"]["args"] == ["-y", "server-a"]
    assert out["alpha"]["env"] == {"KEY": "value", "OTHER": "x"}
    assert out["beta"]["command"] == "/usr/bin/beta"
    assert "unrelated" not in out


def test_toml_fallback_handles_multiline_array():
    text = '[mcp_servers.wrapped]\nargs = [\n  "-y",\n  "server-b"\n]\ncommand = "npx"\n'
    out = ie.parse_mcp_servers_toml_fallback(text)
    assert out["wrapped"]["args"] == ["-y", "server-b"]


# --- bad source/workspace paths raise a recoverable error -------------------


def test_bad_source_path_raises(tmp_path, isolated_skills, isolated_mcp):
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(ie.ExternalImportError):
        ie.import_claude_project(str(tmp_path / "does-not-exist"), str(ws))


def test_bad_workspace_path_raises(tmp_path, isolated_skills, isolated_mcp):
    src = _make_claude_project(str(tmp_path / "src"))
    with pytest.raises(ie.ExternalImportError):
        ie.import_claude_project(src, str(tmp_path / "no-such-workspace"))


# --- HTTP route --------------------------------------------------------


@pytest.fixture()
def client(isolated_skills, isolated_mcp):
    from server.onboarding.routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_route_imports_claude_project_and_auto_detects(tmp_path, client):
    src = _make_claude_project(str(tmp_path / "src"))
    ws = tmp_path / "ws"
    ws.mkdir()

    resp = client.post(
        "/api/onboarding/import",
        json={"source_path": src, "workspace_path": str(ws)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_type"] == "claude"
    assert body["instructions"]["status"] == "written"
    assert [s["name"] for s in body["skills"]["copied"]] == ["alpha"]
    assert body["mcp"]["added"] == ["filesystem"]


def test_route_rejects_bogus_source_path(tmp_path, client):
    ws = tmp_path / "ws"
    ws.mkdir()
    resp = client.post(
        "/api/onboarding/import",
        json={"source_path": str(tmp_path / "nope"), "workspace_path": str(ws)},
    )
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_route_requires_explicit_type_when_undetectable(tmp_path, client):
    src = tmp_path / "src"
    src.mkdir()  # no CLAUDE.md/.claude or AGENTS.md/.codex markers
    ws = tmp_path / "ws"
    ws.mkdir()
    resp = client.post(
        "/api/onboarding/import",
        json={"source_path": str(src), "workspace_path": str(ws)},
    )
    assert resp.status_code == 400
    assert "source_type" in resp.json()["error"]


def test_route_honors_explicit_source_type_override(tmp_path, client):
    # Source looks like a Claude project (has CLAUDE.md) but the caller pins
    # "codex" explicitly — the explicit value wins over auto-detection, so
    # the Claude-only files are ignored and nothing Codex-shaped is found.
    src = _make_claude_project(str(tmp_path / "src"))
    ws = tmp_path / "ws"
    ws.mkdir()

    resp = client.post(
        "/api/onboarding/import",
        json={"source_path": src, "workspace_path": str(ws), "source_type": "codex"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_type"] == "codex"
    assert body["instructions"]["status"] == "skipped"  # looked for AGENTS.md, not CLAUDE.md
