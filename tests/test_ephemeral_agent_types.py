"""Part 2: ephemeral, LLM-created agent types (spawn_agent's agent_definition).

Follows the monkeypatch-run_agent conventions of test_spawn_agent_team_scaffold.py
and test_detached_agents.py.
"""

import asyncio
import json
from types import SimpleNamespace

from server import agent_tools
from server.agents.config import get_agent_config
from server.agents.custom_config import register_ephemeral_type


class _Bus:
    def __init__(self):
        self.events = []

    def publish(self, channel, ev):
        self.events.append((channel, ev))


def test_register_ephemeral_type_defaults_to_read_only_when_unspecified():
    # SECURITY: nobody reviews an ephemeral definition before it runs, so the
    # DEFAULT (neither tools nor read_only given) must fail closed.
    _key, config, meta = register_ephemeral_type(
        "sess-x", {"name": "mystery_agent", "description": "no tools given"}
    )
    assert config.read_only is True
    assert meta["read_only"] is True
    assert meta["name"] == "mystery_agent"


def test_register_ephemeral_type_honors_explicit_tools_whitelist():
    _key, config, _meta = register_ephemeral_type(
        "sess-y",
        {
            "name": "pdf-bot",
            "description": "reads PDFs",
            "tools": ["ws_read_file", "ws_grep"],
        },
    )
    assert config.read_only is False
    assert config.allowed_tools == frozenset({"ws_read_file", "ws_grep"})
    # Name sanitized (hyphen kept, since it's filename-safe).
    assert config.agent_type == "pdf-bot"


def test_register_ephemeral_type_strips_write_tool_when_read_only_requested():
    # SECURITY: read_only: true + a tools list naming a write tool -> the
    # write tool is stripped at construction time, not just at filter time.
    _key, config, _meta = register_ephemeral_type(
        "sess-z",
        {
            "name": "sneaky",
            "description": "tries to smuggle a write tool",
            "read_only": True,
            "tools": ["ws_write_file", "ws_read_file"],
        },
    )
    assert config.allowed_tools == frozenset({"ws_read_file"})


def test_get_agent_config_resolves_ephemeral_by_resolution_key(monkeypatch):
    # Neutralize the repo's real config.example.json agent_limits overrides
    # (a "default" block rewrites max_turns/deadline_seconds for every type),
    # so identity holds: get_agent_config returns the SAME object with no
    # override changes applied.
    monkeypatch.setattr("server.infrastructure.config.load_config", lambda *a, **k: {})
    resolution_key, config, _meta = register_ephemeral_type(
        "sess-lookup", {"name": "lookup_bot", "description": "d", "read_only": True}
    )
    resolved = get_agent_config(resolution_key)
    assert resolved is config
    assert resolved.agent_type == "lookup_bot"


def test_ephemeral_inline_spawn_runs_and_surfaces_type(monkeypatch):
    bus = _Bus()
    monkeypatch.setattr("server.agents.event_bus.event_bus", bus)
    monkeypatch.setattr("server.agent_tools.spawn._record_agent_cost", lambda *a, **k: None)
    captured = {}

    async def _fake_run_agent(task, **kwargs):
        captured.update(kwargs, task=task)
        resolved = get_agent_config(kwargs["agent_type"])
        return SimpleNamespace(
            agent_id="a1",
            agent_type=resolved.agent_type,
            status="completed",
            turns_used=2,
            tools_called=["ws_read_file"],
            usage={},
            output="done reading",
        )

    monkeypatch.setattr("server.agents.runtime.run_agent", _fake_run_agent)

    out = json.loads(
        asyncio.run(
            agent_tools.execute_spawn_agent(
                {
                    "task": "summarize the readme",
                    "agent_definition": {
                        "name": "readme_summarizer",
                        "description": "summarizes docs",
                        "read_only": True,
                    },
                },
                "sess-eph-3",
                "model-x",
            )
        )
    )

    assert out["agent_type"] == "readme_summarizer"
    assert out["ephemeral_type"]["name"] == "readme_summarizer"
    assert out["ephemeral_type"]["description"] == "summarizes docs"
    # The lookup key threaded to run_agent is a synthesized, unique key — not
    # the clean display name (so multiple ephemeral spawns never collide).
    assert captured["agent_type"] != "readme_summarizer"
    assert captured["agent_type"].startswith("readme_summarizer~")


def test_ephemeral_detached_without_worktree_is_forced_read_only(monkeypatch):
    # SECURITY INVARIANT: an ephemeral, non-read_only definition spawned
    # detached without isolation="worktree" must be forced read-only by the
    # EXACT SAME existing guardrail detached named types already go through
    # (_start_detached_from_tool's `read_only = isolation != "worktree"`) — no
    # parallel/laxer path for ephemeral types.
    captured = {}

    def _fake_start(task, **kwargs):
        captured.update(kwargs, task=task)
        return "task-eph-1"

    monkeypatch.setattr("server.tasks.agents.start_detached_agent", _fake_start)

    out = json.loads(
        asyncio.run(
            agent_tools.execute_spawn_agent(
                {
                    "task": "write a report",
                    "detach": True,
                    "agent_definition": {
                        "name": "writer_bot",
                        "description": "writes files",
                        "tools": ["ws_write_file", "ws_read_file"],
                        "read_only": False,
                    },
                },
                "sess-eph-1",
                "model-x",
            )
        )
    )

    assert captured["read_only"] is True, "detached without worktree isolation must be read-only"
    assert out["ephemeral_type"]["name"] == "writer_bot"
    # meta reflects what the definition itself resolved to (not the detached
    # forcing, which is a separate runtime decision) — kept distinct so the
    # model can tell the two apart.
    assert out["ephemeral_type"]["read_only"] is False


def test_ephemeral_worktree_isolation_preserves_write_tools(monkeypatch):
    # The opt-in counterpart: isolation="worktree" is the existing guardrail
    # that lets a detached, non-read_only agent actually write (into its own
    # worktree) — verify an ephemeral type gets that exact same treatment.
    captured = {}

    def _fake_start(task, **kwargs):
        captured.update(kwargs, task=task)
        return "task-eph-2"

    monkeypatch.setattr("server.tasks.agents.start_detached_agent", _fake_start)

    asyncio.run(
        agent_tools.execute_spawn_agent(
            {
                "task": "refactor files",
                "detach": True,
                "isolation": "worktree",
                "agent_definition": {
                    "name": "refactor_bot",
                    "description": "refactors files",
                    "tools": ["ws_write_file", "ws_read_file"],
                    "read_only": False,
                },
            },
            "sess-eph-2",
            "model-x",
        )
    )

    assert captured["read_only"] is False
    assert captured["isolation"] == "worktree"
    resolved = get_agent_config(captured["agent_type"])
    assert resolved.read_only is False
    assert resolved.allowed_tools == frozenset({"ws_write_file", "ws_read_file"})
