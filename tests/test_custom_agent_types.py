"""Part 1: persistent custom agent types loaded from .whisper/agents/*.md.

Covers the YAML-frontmatter loader, the PROJECT-over-USER precedence
(mirroring server/infrastructure/config.py's PROJECT-over-USER layering for
regular settings), custom-over-builtin precedence in get_agent_config, and the
filename-safety guard used when writing a promoted type back out.
"""

import os

from server.agents.config import get_agent_config
from server.agents.custom_config import (
    build_agent_config,
    load_custom_agent_types,
    safe_type_name,
)
from server.workspace.state import (
    get_workspace_path,
    reset_workspace_override,
    set_workspace_override,
)


def _write(directory, name, content):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, f"{name}.md"), "w", encoding="utf-8") as f:
        f.write(content)


def test_load_custom_agent_type_from_md_file(tmp_path, monkeypatch):
    monkeypatch.setattr("server.infrastructure.paths.config_dir", lambda: str(tmp_path / "home"))
    _write(
        tmp_path / "project" / ".whisper" / "agents",
        "pdf_extractor",
        (
            "---\n"
            "name: pdf_extractor\n"
            "description: Extracts structured text from PDFs\n"
            "tools:\n"
            "  - ws_read_file\n"
            "  - ws_grep\n"
            "max_turns: 12\n"
            "model: sonnet4.5\n"
            "---\n"
            "You are a focused PDF extraction agent. Read files and summarize "
            "their content.\n"
        ),
    )

    types = load_custom_agent_types(workspace_path=str(tmp_path / "project"))

    assert "pdf_extractor" in types
    cfg = types["pdf_extractor"]
    assert cfg.agent_type == "pdf_extractor"
    assert cfg.allowed_tools == frozenset({"ws_read_file", "ws_grep"})
    # tools given, no explicit read_only -> not forced read-only (matches the
    # built-in memory_extractor pattern: an explicit whitelist without
    # read_only implies the author wants exactly those tools, writes included).
    assert cfg.read_only is False
    assert cfg.max_turns == 12
    assert cfg.model == "sonnet4.5"
    assert "PDF extraction agent" in (cfg.system_prompt or "")


def test_load_custom_agent_type_read_only_flag(tmp_path, monkeypatch):
    monkeypatch.setattr("server.infrastructure.paths.config_dir", lambda: str(tmp_path / "home"))
    _write(
        tmp_path / "project" / ".whisper" / "agents",
        "auditor",
        (
            "---\n"
            "name: auditor\n"
            "description: Reviews code without touching it\n"
            "read_only: true\n"
            "---\n"
            "Audit the code for issues; never modify anything.\n"
        ),
    )

    types = load_custom_agent_types(workspace_path=str(tmp_path / "project"))
    cfg = types["auditor"]

    assert cfg.read_only is True
    assert cfg.allowed_tools is None
    assert "Audit the code" in cfg.system_prompt


def test_project_custom_type_overrides_user_type_same_name(tmp_path, monkeypatch):
    # PROJECT wins over USER on a name collision, matching
    # server/infrastructure/config.py's PROJECT-over-USER precedence for
    # regular settings.
    user_home = tmp_path / "user_home"
    project = tmp_path / "project"
    monkeypatch.setattr("server.infrastructure.paths.config_dir", lambda: str(user_home))

    _write(
        user_home / ".whisper" / "agents",
        "reviewer",
        "---\nname: reviewer\ndescription: user-level\nmax_turns: 5\n---\nUser body.\n",
    )
    _write(
        project / ".whisper" / "agents",
        "reviewer",
        "---\nname: reviewer\ndescription: project-level\nmax_turns: 99\n---\nProject body.\n",
    )

    types = load_custom_agent_types(workspace_path=str(project))

    assert types["reviewer"].max_turns == 99
    assert "Project body" in types["reviewer"].system_prompt


def test_user_type_used_when_no_project_override(tmp_path, monkeypatch):
    user_home = tmp_path / "user_home"
    monkeypatch.setattr("server.infrastructure.paths.config_dir", lambda: str(user_home))
    _write(
        user_home / ".whisper" / "agents",
        "solo",
        "---\nname: solo\ndescription: user only\nmax_turns: 7\n---\nUser-only body.\n",
    )

    # No project dir at all (or a project with no agents/ dir of its own).
    types = load_custom_agent_types(workspace_path=str(tmp_path / "empty_project"))

    assert types["solo"].max_turns == 7


def test_load_custom_agent_types_default_resolves_current_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr("server.infrastructure.paths.config_dir", lambda: str(tmp_path / "home"))
    _write(
        tmp_path / "proj" / ".whisper" / "agents",
        "foo",
        "---\nname: foo\ndescription: d\nread_only: true\n---\nBody.\n",
    )
    token = set_workspace_override(str(tmp_path / "proj"))
    try:
        assert get_workspace_path() == str(tmp_path / "proj")
        types = load_custom_agent_types()  # no explicit workspace_path
    finally:
        reset_workspace_override(token)

    assert "foo" in types


def test_custom_type_overrides_builtin_by_same_name(tmp_path, monkeypatch):
    # Name collision between a built-in type ("explore") and a custom file:
    # the custom file wins (get_agent_config checks custom types first).
    monkeypatch.setattr("server.infrastructure.paths.config_dir", lambda: str(tmp_path / "home"))
    # Neutralize the repo's real config.example.json agent_limits overrides
    # (a "default" block there rewrites max_turns/deadline_seconds for every
    # type) so this test only sees what get_agent_config resolves from the
    # custom file itself.
    monkeypatch.setattr("server.infrastructure.config.load_config", lambda *a, **k: {})
    _write(
        tmp_path / "project" / ".whisper" / "agents",
        "explore",
        (
            "---\nname: explore\ndescription: custom explore\nmax_turns: 3\n"
            "read_only: true\n---\nCustom explore body.\n"
        ),
    )

    token = set_workspace_override(str(tmp_path / "project"))
    try:
        cfg = get_agent_config("explore")
    finally:
        reset_workspace_override(token)

    assert cfg.max_turns == 3
    assert "Custom explore body" in (cfg.system_prompt or "")


def test_no_custom_type_falls_back_to_builtin(tmp_path, monkeypatch):
    monkeypatch.setattr("server.infrastructure.paths.config_dir", lambda: str(tmp_path / "home"))
    monkeypatch.setattr("server.infrastructure.config.load_config", lambda *a, **k: {})

    token = set_workspace_override(str(tmp_path / "empty_project"))
    try:
        cfg = get_agent_config("verify")
    finally:
        reset_workspace_override(token)

    assert cfg.agent_type == "verify"
    assert cfg.max_turns == 30  # built-in verify preset, untouched


def test_safe_type_name_rejects_path_traversal_and_separators():
    assert safe_type_name("../../etc/passwd") is None
    assert safe_type_name("a/b") is None
    assert safe_type_name("") is None
    assert safe_type_name("good_name-1") == "good_name-1"


def test_build_agent_config_defaults_and_tools_whitelist():
    cfg = build_agent_config("plain", default_read_only=False)
    assert cfg.read_only is False
    assert cfg.allowed_tools is None

    cfg2 = build_agent_config("curated", tools=["ws_read_file", "ws_grep"], default_read_only=False)
    assert cfg2.read_only is False
    assert cfg2.allowed_tools == frozenset({"ws_read_file", "ws_grep"})


def test_build_agent_config_strips_write_tools_when_read_only():
    # SECURITY: read_only=True + a tools whitelist containing a write tool ->
    # the write tool never makes it into allowed_tools at all (belt-and-
    # suspenders alongside the filter_tools_for_agent runtime fix).
    cfg = build_agent_config("sneaky", tools=["ws_write_file", "ws_read_file"], read_only=True)
    assert cfg.read_only is True
    assert cfg.allowed_tools == frozenset({"ws_read_file"})
