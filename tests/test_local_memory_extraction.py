"""Local-path auto-memory extraction + persistent extraction throttle.

Two gaps closed here:

1. The on-device (local model) chat path never fed auto-memory: only the
   cloud paths (chat/routes.py, openai_bedrock/stream.py) spawned
   ``maybe_extract_memory`` at end of turn. The local path now runs the same
   hook, skipping gracefully when the app is fully offline (model_mode=local,
   the extraction agent needs a cloud model).

2. The every-N-turns throttle counter was in-memory only, so a server restart
   reset the cadence and short sessions never accumulated enough turns to
   extract. It is now persisted next to the cursor in the global tier's
   .cursor.json.
"""

import asyncio
import json

import pytest

import server.infrastructure.feature_flags as FF
import server.local.runtime as L
import server.local.server_stream as STREAM
import server.memory.extract as XT
import server.memory.memdir as MD


@pytest.fixture(autouse=True)
def _seed_local_registry(monkeypatch):
    """A fresh checkout ships no local chat models (config-driven registry,
    empty by default). Seed the two recommended entries these tests reference
    by name, so they don't silently depend on the developer's real,
    gitignored config.json. A test that needs a different registry shape
    (empty, config-only, etc.) overrides _config_local_models itself, which
    wins since it runs after this fixture."""
    from server.local import registry as _reg

    monkeypatch.setattr(
        _reg,
        "_config_local_models",
        lambda: {
            key: dict(_reg.RECOMMENDED_LOCAL_MODELS[key])
            for key in ("local_gemma", "local_gemma_coder")
        },
    )


def _drain(agen_factory):
    async def run():
        return [c async for c in agen_factory()]

    return asyncio.run(run())


def _serve(monkeypatch, rounds=(([], []),)):
    """Stub the subprocess + HTTP so a turn runs offline.

    ``rounds`` is a sequence of (pieces, calls) per round, where pieces are
    ("text"|"thinking", str) — the shape _stream_round yields.
    """
    from server.local import serving

    # Facade seam — see the route; the engine-level function needs a real
    # binary and installed weights on the machine, absent on CI runners.
    monkeypatch.setattr(serving, "ensure_serving", lambda *a, **k: "http://stub")
    box = {"i": 0}

    async def fake_round(base_url, payload):
        i = min(box["i"], len(rounds) - 1)
        box["i"] += 1
        pieces, calls = rounds[i]
        for kind, piece in pieces:
            yield (kind, piece)
        yield ("done", {"calls": list(calls)})

    monkeypatch.setattr(STREAM, "_stream_round", fake_round)


def _messages(n):
    return [{"role": "user", "content": f"m{i}"} for i in range(n)]


class _AgentResult:
    status = "completed"
    turns_used = 1
    tools_called = ["memory_write"]
    output = ""


@pytest.fixture
def mem_dirs(tmp_path, monkeypatch):
    """Isolated memory roots + auto_memory forced on."""
    project_base = tmp_path / "memory"
    global_dir = tmp_path / "global_memory"
    monkeypatch.setattr(MD, "MEMORY_BASE", str(project_base))
    monkeypatch.setattr(MD, "GLOBAL_MEMORY_DIR", str(global_dir))
    monkeypatch.setattr(FF, "is_enabled", lambda flag: flag == "auto_memory")
    return {"project_base": project_base, "global": global_dir}


@pytest.fixture
def fresh_extract_state():
    XT._cursors.clear()
    XT._turn_counters.clear()
    XT._inflight.clear()
    yield
    XT._cursors.clear()
    XT._turn_counters.clear()
    XT._inflight.clear()


# ── Local turn wiring: memory hooks at end of turn (engine path) ─────────────


def _local_turn(monkeypatch, msgs, session_id, ws_path=None, pending=False):
    """Run one local engine turn via local_chat_response with the llama pieces
    stubbed. ``pending=True`` fakes an approval pause in the tool pipeline."""
    import server.tool_executor as TE
    from server.local import route as R

    async def fake_batch(tool_uses, **kw):
        return list(tool_uses)

    async def fake_process(states, budget_fn, **kw):
        results = [{"type": "tool_result", "tool_use_id": t["id"], "content": "ok"} for t in states]
        return (results, [], pending, False)

    monkeypatch.setattr(TE, "execute_tool_batch", fake_batch)
    monkeypatch.setattr(TE, "process_tool_results", fake_process)

    resp = R.local_chat_response(
        model_key="local_gemma",
        body={},
        messages=msgs,
        session_id=session_id,
        approved_tool_result=None,
        transcript="",
        whisper_md_context="",
        memory_context="",
        session_memory_context="",
        plan_mode=False,
        mode="default",
        ws_path=ws_path,
        session_approvals={},
        session_denials={},
        session_config={},
    )
    assert resp is not None
    return _drain(lambda: resp.body_iterator)


def test_plain_turn_fires_memory_hooks_with_ws_path(monkeypatch):
    _serve(monkeypatch, rounds=((([("text", "hi")]), []),))
    calls = []
    monkeypatch.setattr(STREAM, "_spawn_memory_hooks", lambda *a: calls.append(a))

    msgs = [{"role": "user", "content": "hello"}]
    _local_turn(monkeypatch, msgs, "s1", ws_path="/ws")

    assert calls == [("local_gemma", msgs, "s1", "/ws")]


def test_turn_skips_memory_hooks_on_error(monkeypatch):
    """A failed turn must not record memory. With no fallback runtime, an
    unavailable model server is exactly this case."""
    from server.local import serving

    def boom(*a, **k):
        raise RuntimeError("llama-server unavailable")

    monkeypatch.setattr(serving, "ensure_serving", boom)
    calls = []
    monkeypatch.setattr(STREAM, "_spawn_memory_hooks", lambda *a: calls.append(a))

    _local_turn(monkeypatch, _messages(1), "s2")
    assert calls == []


def test_hooks_skipped_on_pause_fired_on_completion(monkeypatch):
    """Hooks must not fire on a half-finished (approval-paused) turn, only
    when a turn completes — the engine's pause branch returns before hooks."""
    from server.chat.engine.pause import paused_sessions

    calls = []
    monkeypatch.setattr(STREAM, "_spawn_memory_hooks", lambda *a: calls.append(a))

    # Paused turn: a tool round whose pipeline reports a pending approval.
    _serve(
        monkeypatch,
        rounds=(
            ([], [{"id": "w1", "name": "ws_write_file", "input": {}}]),
            ([("text", "after")], []),
        ),
    )
    paused_sessions.pop("s-pause", None)
    _local_turn(monkeypatch, _messages(1), "s-pause", pending=True)
    assert calls == []
    assert "s-pause" in paused_sessions
    paused_sessions.pop("s-pause", None)

    # Completed turn: hooks fire exactly once.
    _serve(monkeypatch, rounds=((([("text", "done")]), []),))
    _local_turn(monkeypatch, _messages(1), "s-done")
    assert len(calls) == 1


def test_memory_hooks_call_session_update_extraction_and_dream(monkeypatch):
    seen = []
    monkeypatch.setattr(
        STREAM, "_spawn_session_update", lambda mk, msgs, sid: seen.append(("session", sid))
    )
    monkeypatch.setattr(
        STREAM, "_spawn_extraction", lambda mk, msgs, sid, ws: seen.append(("extract", sid, ws))
    )
    monkeypatch.setattr(STREAM, "_spawn_dream", lambda mk, ws: seen.append(("dream", ws)))
    STREAM._spawn_memory_hooks("local_gemma", _messages(1), "s3", "/ws")
    assert seen == [("session", "s3"), ("extract", "s3", "/ws"), ("dream", "/ws")]


# ── _spawn_extraction gating ─────────────────────────────────────────────────


def _capture_extraction(monkeypatch):
    """Route the spawned extraction coroutine into a list of kwargs."""
    import server.infrastructure.async_tasks as AT

    calls = []

    async def fake_extract(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(XT, "maybe_extract_memory", fake_extract)
    monkeypatch.setattr(AT, "spawn", lambda coro, *, name=None: asyncio.run(coro))
    return calls


def test_extraction_hook_fires_in_hybrid_mode(monkeypatch):
    import server.infrastructure.model_mode as MM

    monkeypatch.setattr(FF, "is_enabled", lambda flag: flag == "auto_memory")
    monkeypatch.setattr(MM, "current_mode", lambda config=None: "hybrid")
    calls = _capture_extraction(monkeypatch)

    msgs = _messages(3)
    STREAM._spawn_extraction("local_gemma", msgs, "s4", "/ws")

    assert len(calls) == 1
    assert calls[0]["messages"] == msgs
    assert calls[0]["session_id"] == "s4"
    assert calls[0]["ws_path"] == "/ws"
    # The local sentinel id is threaded (and ignored by the extractor agent).
    assert calls[0]["model_id"] == L.local_model_meta("local_gemma").get("id", "")


def test_extraction_hook_skips_when_fully_offline(monkeypatch):
    import server.infrastructure.model_mode as MM

    monkeypatch.setattr(FF, "is_enabled", lambda flag: flag == "auto_memory")
    monkeypatch.setattr(MM, "current_mode", lambda config=None: "local")
    calls = _capture_extraction(monkeypatch)

    STREAM._spawn_extraction("local_gemma", _messages(3), "s5", None)
    assert calls == []


def test_extraction_hook_gated_on_auto_memory_flag(monkeypatch):
    import server.infrastructure.model_mode as MM

    monkeypatch.setattr(FF, "is_enabled", lambda flag: False)
    monkeypatch.setattr(MM, "current_mode", lambda config=None: "hybrid")
    calls = _capture_extraction(monkeypatch)

    STREAM._spawn_extraction("local_gemma", _messages(3), "s6", None)
    assert calls == []


# ── Persistent extraction throttle ───────────────────────────────────────────


def _fake_agent(monkeypatch):
    import server.agents.runtime as RT

    calls = []

    async def fake_run_agent(task, **kwargs):
        calls.append(kwargs.get("session_id"))
        return _AgentResult()

    monkeypatch.setattr(RT, "run_agent", fake_run_agent)
    return calls


def _restart():
    """Simulate a server restart: all in-memory extraction state is gone."""
    XT._cursors.clear()
    XT._turn_counters.clear()
    XT._inflight.clear()


def test_turn_counter_survives_restart(mem_dirs, fresh_extract_state, monkeypatch):
    """Two turns before a restart + one after must trigger extraction; the old
    in-memory counter restarted from zero and needed three MORE turns."""
    calls = _fake_agent(monkeypatch)

    for _ in range(2):
        asyncio.run(
            XT.maybe_extract_memory(
                messages=_messages(4), session_id="r1", ws_path=None, model_id="m"
            )
        )
    assert calls == []
    data = json.loads((mem_dirs["global"] / ".cursor.json").read_text())
    assert data["turns"]["r1"] == 2

    _restart()
    asyncio.run(
        XT.maybe_extract_memory(messages=_messages(4), session_id="r1", ws_path=None, model_id="m")
    )
    assert calls == ["r1"]


def test_turn_counter_resets_after_extraction(mem_dirs, fresh_extract_state, monkeypatch):
    calls = _fake_agent(monkeypatch)

    for _ in range(3):
        asyncio.run(
            XT.maybe_extract_memory(
                messages=_messages(4), session_id="r2", ws_path=None, model_id="m"
            )
        )
    assert calls == ["r2"]
    data = json.loads((mem_dirs["global"] / ".cursor.json").read_text())
    assert data["turns"]["r2"] == 0

    # After a restart the persisted zero keeps the cadence: two more turns stay
    # throttled, the third fires again (with new messages past the cursor).
    _restart()
    for _ in range(3):
        asyncio.run(
            XT.maybe_extract_memory(
                messages=_messages(8), session_id="r2", ws_path=None, model_id="m"
            )
        )
    assert calls == ["r2", "r2"]


def test_cursor_save_preserves_other_sessions_turn_counters(
    mem_dirs, fresh_extract_state, monkeypatch
):
    """A cursor claim (extraction firing for one session) must not drop the
    persisted turn counters of other sessions from the shared state file."""
    calls = _fake_agent(monkeypatch)

    # Session A accumulates one throttled turn...
    asyncio.run(
        XT.maybe_extract_memory(messages=_messages(4), session_id="A", ws_path=None, model_id="m")
    )
    # ...then session B extracts (throttle reset + cursor claim, two saves).
    for _ in range(3):
        asyncio.run(
            XT.maybe_extract_memory(
                messages=_messages(4), session_id="B", ws_path=None, model_id="m"
            )
        )
    assert calls == ["B"]

    data = json.loads((mem_dirs["global"] / ".cursor.json").read_text())
    assert data["turns"]["A"] == 1
    assert data["turns"]["B"] == 0
    assert data["sessions"]["B"] == 4


def test_legacy_cursor_file_without_turns_key(mem_dirs, fresh_extract_state, monkeypatch):
    """A pre-existing sessions-only .cursor.json loads cleanly: turns default
    to 0 and the legacy cursors are preserved on rewrite."""
    calls = _fake_agent(monkeypatch)
    mem_dirs["global"].mkdir(parents=True, exist_ok=True)
    (mem_dirs["global"] / ".cursor.json").write_text(json.dumps({"sessions": {"old": 4}}))

    asyncio.run(
        XT.maybe_extract_memory(messages=_messages(4), session_id="old", ws_path=None, model_id="m")
    )
    assert calls == []  # first counted turn, throttled

    data = json.loads((mem_dirs["global"] / ".cursor.json").read_text())
    assert data["sessions"]["old"] == 4  # legacy cursor carried through
    assert data["turns"]["old"] == 1


def test_persisted_turns_capped_like_cursors(mem_dirs, fresh_extract_state):
    mem_dirs["global"].mkdir(parents=True, exist_ok=True)
    gdir = str(mem_dirs["global"])
    for i in range(XT._MAX_CURSOR_SESSIONS + 5):
        XT._save_state(gdir, f"s{i}", turns=1)

    data = json.loads((mem_dirs["global"] / ".cursor.json").read_text())
    assert len(data["turns"]) == XT._MAX_CURSOR_SESSIONS
    # Oldest entries dropped first
    assert "s0" not in data["turns"]
    assert f"s{XT._MAX_CURSOR_SESSIONS + 4}" in data["turns"]
