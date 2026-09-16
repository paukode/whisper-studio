"""server.memory.review_fork: the post-turn learning review replays the turn's
own request (cache parity), runs only memory and skill tools, records usage,
and is routed from the extraction hook when a fork is available."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from server.chat.engine.events import RoundResult, Usage
from server.memory import extract, review_fork


class FakeAdapter:
    provider = "anthropic"
    cached_in_input = False

    def __init__(self):
        self.calls: list[dict] = []

    async def stream_round(self, messages, tools, core_count, round_num, is_last_round):
        self.calls.append({"messages": list(messages), "tools": tools, "round": round_num})
        if len(self.calls) == 1:
            yield RoundResult(
                stop_reason="tool_use",
                content=[
                    {"type": "text", "text": "Saving."},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "memory_write",
                        "input": {"filename": "a.md"},
                    },
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "ws_run_command",
                        "input": {"command": "rm -rf"},
                    },
                ],
                usage=Usage(input_tokens=5, output_tokens=7, cache_read_tokens=9000),
            )
        else:
            yield RoundResult(
                stop_reason="end_turn",
                content=[{"type": "text", "text": "Nothing to save."}],
                usage=Usage(input_tokens=1, output_tokens=2, cache_read_tokens=9100),
            )


def _fork(adapter, messages=None):
    return review_fork.ReviewFork(
        adapter=adapter,
        tools=[{"name": "memory_write"}, {"name": "ws_run_command"}],
        core_count=1,
        messages=messages
        or [
            {"role": "user", "content": "remember I prefer tabs"},
            {"role": "assistant", "content": [{"type": "text", "text": "Noted."}]},
        ],
        model_key="opus5.0",
        model_id="model-id",
        session_id="s1",
        ws_path=None,
        loop=None,
        executor=None,
    )


@pytest.fixture
def _plumbing(monkeypatch):
    dispatched = []

    async def fake_route(tool_name, call_input, **kw):
        dispatched.append((tool_name, call_input.get("__agent__")))
        return ("written", [])

    recorded = []
    monkeypatch.setattr("server.tool_router.route_tool", fake_route)
    monkeypatch.setattr("server.costs.tracker.record_turn", lambda **kw: recorded.append(kw))
    monkeypatch.setattr("server.chat.infra._estimate_cost", lambda *a, **k: 0.0)
    return dispatched, recorded


def test_fork_replays_the_turn_and_only_runs_allowed_tools(_plumbing):
    dispatched, recorded = _plumbing
    adapter = FakeAdapter()
    fork = _fork(adapter)
    stats = asyncio.run(review_fork.run_review(fork, since_index=0))

    assert stats["ended"] == "done" and stats["rounds"] == 2
    # Cache parity: the first request is the turn's messages plus ONE review message,
    # with the identical tools list object.
    first = adapter.calls[0]
    assert first["messages"][: len(fork.messages)] == fork.messages
    assert len(first["messages"]) == len(fork.messages) + 1
    assert first["messages"][-1]["role"] == "user"
    assert "Post-turn learning review" in first["messages"][-1]["content"]
    assert first["tools"] is fork.tools
    # Only memory_write reached the router, stamped as an agent call.
    assert dispatched == [("memory_write", True)]
    results = adapter.calls[1]["messages"][-1]["content"]
    by_id = {r["tool_use_id"]: r["content"] for r in results}
    assert by_id["t1"] == "written"
    assert "not available during the post-turn review" in by_id["t2"]
    # Usage is recorded per round under the review turn numbers.
    assert [r["turn_number"] for r in recorded] == [900, 901]
    assert stats["cache_read_tokens"] == 18100


def test_supports_fork_and_prompt_contents():
    assert review_fork.supports_fork(SimpleNamespace(provider="anthropic"))
    assert review_fork.supports_fork(SimpleNamespace(provider="openai"))
    assert not review_fork.supports_fork(SimpleNamespace(provider="local"))
    from server.memory.prompts import build_learning_review_prompt

    p = build_learning_review_prompt(recent_user_turns=2, review_skills=True, project_scope=True)
    assert "last 2 user turn" in p and "skill_manage" in p and "scope='project'" in p
    assert p.rstrip().endswith("Nothing to save.")
    assert "skill_manage" not in build_learning_review_prompt(review_skills=False)


def test_fork_route_follows_flag_and_auxiliary_setting(monkeypatch):
    fork = _fork(FakeAdapter())
    flags = {"learning_review_fork": True}
    monkeypatch.setattr(
        "server.infrastructure.feature_flags.is_enabled", lambda name: flags.get(name, False)
    )
    monkeypatch.setattr("server.infrastructure.auxiliary._map", lambda config=None: {})
    assert extract._fork_route(fork) is True
    monkeypatch.setattr(
        "server.infrastructure.auxiliary._map", lambda config=None: {"learning_review": "opus5.0"}
    )
    assert extract._fork_route(fork) is True
    monkeypatch.setattr(
        "server.infrastructure.auxiliary._map", lambda config=None: {"learning_review": "haiku"}
    )
    assert extract._fork_route(fork) is False
    monkeypatch.setattr("server.infrastructure.auxiliary._map", lambda config=None: {})
    flags["learning_review_fork"] = False
    assert extract._fork_route(fork) is False
    flags["learning_review_fork"] = True
    assert extract._fork_route(_fork(SimpleNamespace(provider="local"))) is False


def test_extraction_hook_prefers_the_fork(tmp_path, monkeypatch):
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    monkeypatch.setattr(extract, "ensure_global_memory_dir", lambda: str(global_dir))
    monkeypatch.setattr(extract, "ensure_memory_dir", lambda ws: None)
    monkeypatch.setattr(extract, "DEFAULT_EXTRACT_INTERVAL", 1)
    monkeypatch.setattr(extract, "_fork_route", lambda fork: True)
    seen = {}

    async def fake_fork(fork, *, since_index, global_dir, project_dir, session_id):
        seen.update(since_index=since_index, session_id=session_id, fork=fork)

    async def fake_legacy(*a, **k):
        seen["legacy"] = True

    monkeypatch.setattr(extract, "_run_review_fork", fake_fork)
    monkeypatch.setattr(extract, "_run_extraction", fake_legacy)
    fork = _fork(FakeAdapter())
    asyncio.run(
        extract.maybe_extract_memory(
            messages=fork.messages, session_id="fork-sess", ws_path=None, model_id="m", fork=fork
        )
    )
    assert seen["since_index"] == 0 and seen["session_id"] == "fork-sess" and seen["fork"] is fork
    assert "legacy" not in seen


def test_runner_builds_a_fork_only_for_forkable_providers(monkeypatch):
    from server.chat.engine import runner

    monkeypatch.setattr("server.infrastructure.feature_flags.is_enabled", lambda name: True)
    ctx = SimpleNamespace(
        adapter=SimpleNamespace(provider="anthropic"),
        model_key="k",
        model_id="id",
        session_id="s",
        ws_path=None,
        loop=None,
        executor=None,
        transcript="",
        current_attachments={},
    )
    msgs = [{"role": "user", "content": "hi"}]
    fork = runner._build_review_fork(
        ctx, [{"name": "x"}], 1, msgs, [{"type": "text", "text": "yo"}]
    )
    assert fork is not None and fork.messages[-1] == {
        "role": "assistant",
        "content": [{"type": "text", "text": "yo"}],
    }
    assert fork.messages[0] == msgs[0] and fork.core_count == 1
    ctx.adapter = SimpleNamespace(provider="local")
    assert runner._build_review_fork(ctx, [], None, msgs, []) is None
