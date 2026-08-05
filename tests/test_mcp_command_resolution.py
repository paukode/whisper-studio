"""Missing MCP command -> actionable error (not a raw errno), and GUI PATH enrichment."""

import asyncio
import os

from server.infrastructure import binaries
from server.mcp import MCPManager


def test_resolve_command_bare_and_absolute(tmp_path):
    # Bare name resolves against the given PATH.
    exe = tmp_path / "mytool"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert MCPManager._resolve_command("mytool", str(tmp_path)) == str(exe)
    assert MCPManager._resolve_command("nope-xyz", str(tmp_path)) is None
    # Absolute must exist + be executable.
    assert MCPManager._resolve_command(str(exe), None) == str(exe)
    assert MCPManager._resolve_command(str(tmp_path / "ghost"), None) is None


def test_start_server_missing_command_sets_friendly_error():
    mgr = MCPManager()
    asyncio.run(mgr.start_server("Broken", {"command": "definitely-not-a-real-binary-xyz"}))
    info = mgr._sessions.get("Broken")
    assert info is not None and info["status"] == "error"
    assert "was not found" in info["error"]
    assert "[Errno 2]" not in info["error"]


def test_enrich_path_noop_without_whisper_home(monkeypatch):
    monkeypatch.delenv("WHISPER_HOME", raising=False)
    before = os.environ.get("PATH", "")
    binaries.enrich_gui_launch_path()
    assert os.environ.get("PATH", "") == before


def test_enrich_path_adds_common_dirs(monkeypatch, tmp_path):
    tooldir = tmp_path / "brew" / "bin"
    tooldir.mkdir(parents=True)
    monkeypatch.setenv("WHISPER_HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(binaries, "_COMMON_TOOL_DIRS", (str(tooldir),))
    # Force the login-shell capture to contribute nothing, exercising the fallback.
    monkeypatch.setattr(
        binaries.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no shell")),
    )
    binaries.enrich_gui_launch_path()
    assert str(tooldir) in os.environ["PATH"].split(os.pathsep)
