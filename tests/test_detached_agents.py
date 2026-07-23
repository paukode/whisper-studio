"""Detached spawn_agent: cap, read-only default, and next-turn injection."""

import asyncio
import json

import pytest

from server.agents import completion_inject
from server.tasks import registry, shell


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(registry, "DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setattr(shell, "OUTPUT_DIR", str(tmp_path / "background_output"))
    yield


def test_spawn_agent_detach_returns_task_id_immediately(monkeypatch):
    from server import agent_tools

    started = {}

    def _fake_start(task, **kwargs):
        started.update(kwargs, task=task)
        return "task123abc"

    monkeypatch.setattr("server.tasks.agents.start_detached_agent", _fake_start)
    out = json.loads(
        asyncio.run(
            agent_tools.execute_spawn_agent(
                {"task": "count the stars", "detach": True, "context": "be brief"},
                "sess-d",
                "global.anthropic.claude-opus-4-8",
                effort_label="high",
            )
        )
    )
    assert out["task_id"] == "task123abc"
    assert out["detached"] is True
    assert "task_status" in out["hint"]
    # safety default: detached agents run read-only, with effort inherited
    assert started["read_only"] is True
    assert started["effort_label"] == "high"
    assert started["session_id"] == "sess-d"
    assert "be brief" in started["task"]


def test_spawn_agent_detach_cap(monkeypatch):
    from server import agent_tools

    for i in range(agent_tools.DETACHED_PER_SESSION_CAP):
        registry.create_task("agent", session_id="sess-cap", title=f"a{i}")
    out = json.loads(
        asyncio.run(
            agent_tools.execute_spawn_agent({"task": "one more", "detach": True}, "sess-cap", None)
        )
    )
    assert "cap reached" in out["error"]


def test_pending_completions_and_injection_leading_block():
    tid = registry.create_task("agent", session_id="s-inj", title="research task")
    registry.finish_task(tid, status="completed", result_text="First part.\n\nFinal answer here.")
    # unrelated kinds/status excluded
    shell_tid = registry.create_task("shell", session_id="s-inj", title="ls")
    registry.finish_task(shell_tid, status="completed", exit_code=0)
    running = registry.create_task("agent", session_id="s-inj", title="still going")

    messages = [{"role": "user", "content": "what happened while I was away?"}]
    n = completion_inject.inject_completions("s-inj", messages)
    assert n == 1
    content = messages[0]["content"]
    assert isinstance(content, list)
    # Leading block carries the update; original text preserved after it.
    assert "Background task updates" in content[0]["text"]
    assert "Final answer here." in content[0]["text"]
    assert tid in content[0]["text"]
    assert content[1]["text"] == "what happened while I was away?"

    # Delivered: a second injection is a no-op.
    messages2 = [{"role": "user", "content": "again?"}]
    assert completion_inject.inject_completions("s-inj", messages2) == 0
    assert registry.get_task(running)["status"] == "running"  # untouched


def test_injection_refuses_non_user_tail():
    tid = registry.create_task("agent", session_id="s-tail", title="t")
    registry.finish_task(tid, status="failed", result_text="boom")
    messages = [{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]
    assert completion_inject.inject_completions("s-tail", messages) == 0
    # not marked delivered — it will inject on the next VALID turn
    assert completion_inject.pending_completions("s-tail") != []


def test_terminal_paragraph_truncation():
    long_tail = "start\n\n" + "x" * 500
    out = completion_inject._terminal_paragraph(long_tail)
    assert len(out) <= completion_inject.SUMMARY_CHARS + 1
    assert out.endswith("…")
    assert completion_inject._terminal_paragraph("") == "(no output)"


def test_run_detached_honors_agent_limits(monkeypatch, tmp_path):
    """The read-only detached path must resolve its config through
    get_agent_config so config.json agent_limits overrides apply — building it
    from the raw AGENT_TYPES table silently ignored the knob."""
    from types import SimpleNamespace

    from server.tasks import agents as tagents

    monkeypatch.setattr(
        "server.infrastructure.config.load_config",
        lambda: {"agent_limits": {"general": {"max_turns": 7, "deadline_seconds": 42}}},
    )
    captured = {}

    async def _fake_run_agent(task, **kwargs):
        captured["config"] = kwargs.get("config")
        return SimpleNamespace(output="done", status="completed")

    monkeypatch.setattr("server.agents.runtime.run_agent", _fake_run_agent)
    tid = registry.create_task("agent", session_id="s-lim", title="t")
    out_path = str(tmp_path / "out.txt")
    asyncio.run(
        tagents._run_detached(tid, "task", "general", "s-lim", "model-x", out_path, None, True)
    )
    cfg = captured["config"]
    assert cfg.read_only is True
    assert cfg.max_turns == 7
    assert cfg.deadline_seconds == 42


def test_run_detached_drains_final_events_into_output_file(monkeypatch, tmp_path):
    """Events queued when the agent returns (its final tool_result/completed)
    must land in the output file — cancelling the pump first dropped them."""
    from types import SimpleNamespace

    from server.agents.event_bus import event_bus
    from server.tasks import agents as tagents

    async def _fake_run_agent(task, **kwargs):
        ch = kwargs["event_channel"]
        event_bus.publish(
            ch, {"phase": "tool_call", "tool_name": "ws_grep", "tool_input_preview": "q"}
        )
        event_bus.publish(ch, {"phase": "completed", "turns_used": 3})
        return SimpleNamespace(output="done", status="completed")

    monkeypatch.setattr("server.agents.runtime.run_agent", _fake_run_agent)
    tid = registry.create_task("agent", session_id="s-drain", title="t")
    out_path = str(tmp_path / "out.txt")
    asyncio.run(
        tagents._run_detached(tid, "task", "general", "s-drain", "model-x", out_path, None, False)
    )
    content = open(out_path).read()
    assert "[tool_call] ws_grep q" in content
    assert "[completed] turns_used=3" in content
