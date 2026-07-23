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
import server.local.stream as STREAM
import server.memory.extract as XT
import server.memory.memdir as MD


def _drain(agen_factory):
    async def run():
        return [c async for c in agen_factory()]

    return asyncio.run(run())


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


# ── Local stream wiring: memory hooks at end of turn ─────────────────────────


def test_plain_stream_fires_memory_hooks_with_ws_path(monkeypatch):
    monkeypatch.setattr(L, "iter_chat", lambda *a, **k: iter([("text", "hi")]))
    calls = []
    monkeypatch.setattr(STREAM, "_spawn_memory_hooks", lambda *a: calls.append(a))

    msgs = [{"role": "user", "content": "hello"}]
    _drain(lambda: STREAM.stream_local_chat("local_gemma", "sys", msgs, "s1", ws_path="/ws"))

    assert calls == [("local_gemma", msgs, "s1", "/ws")]


def test_plain_stream_skips_memory_hooks_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no llama")

    monkeypatch.setattr(L, "iter_chat", boom)
    calls = []
    monkeypatch.setattr(STREAM, "_spawn_memory_hooks", lambda *a: calls.append(a))

    _drain(lambda: STREAM.stream_local_chat("local_gemma", "sys", _messages(1), "s2"))
    assert calls == []


def test_tools_stream_hooks_skipped_on_pause_fired_on_completion(monkeypatch):
    # Mirror of the OpenAI-path regression test: hooks must not fire on a
    # half-finished (approval-paused) turn, only when the turn completes.
    monkeypatch.setattr(L, "supports_tools", lambda key: True)
    monkeypatch.setattr(L, "to_chat_messages", lambda sp, msgs: [{"role": "user", "content": "x"}])
    calls = []
    monkeypatch.setattr(STREAM, "_spawn_memory_hooks", lambda *a: calls.append(a))

    async def _fake_pause(
        model_key, convo, tool_ctx, *, session_id, start_round=0, memory_ctx=None
    ):
        STREAM._local_paused[session_id] = {"convo": convo, "memory_ctx": memory_ctx}
        yield "data: [DONE]\n\n"

    async def _fake_done(model_key, convo, tool_ctx, *, session_id, start_round=0, memory_ctx=None):
        yield "data: [DONE]\n\n"

    msgs = _messages(2)
    STREAM._local_paused.pop("t-pause", None)
    monkeypatch.setattr(STREAM, "_tool_loop", _fake_pause)
    _drain(
        lambda: STREAM.stream_local_chat(
            "local_gemma", "s", msgs, "t-pause", tools=True, ws_path=None
        )
    )
    assert calls == [], "memory hooks must not fire on a pause"
    # The stream threads the hook context into the loop so the pause stash
    # carries it to the resume.
    stash = STREAM._local_paused.pop("t-pause")
    assert stash["memory_ctx"] == {"messages": msgs, "ws_path": None}

    monkeypatch.setattr(STREAM, "_tool_loop", _fake_done)
    _drain(
        lambda: STREAM.stream_local_chat(
            "local_gemma", "s", msgs, "t-done", tools=True, ws_path="/ws"
        )
    )
    assert calls == [("local_gemma", msgs, "t-done", "/ws")]


def test_resume_fires_hooks_on_completion_not_on_repause(monkeypatch):
    # An approval resume is the true end of the turn: the hooks skipped at the
    # pause must fire when the resumed loop completes, and must keep waiting if
    # it pauses again for another approval.
    calls = []
    monkeypatch.setattr(STREAM, "_spawn_memory_hooks", lambda *a: calls.append(a))

    msgs = _messages(2)

    def _stash(session_id):
        STREAM._local_paused[session_id] = {
            "model_key": "local_gemma",
            "convo": [{"role": "user", "content": "x"}],
            "tool_ctx": {},
            "pending_results": [{"type": "tool_result", "tool_use_id": "t1", "content": ""}],
            "names_by_id": {"t1": "ws_write_file"},
            "next_round": 1,
            "memory_ctx": {"messages": msgs, "ws_path": "/ws"},
        }

    async def _fake_repause(
        model_key, convo, tool_ctx, *, session_id, start_round=0, memory_ctx=None
    ):
        STREAM._local_paused[session_id] = {"convo": convo, "memory_ctx": memory_ctx}
        yield "data: [DONE]\n\n"

    async def _fake_done(model_key, convo, tool_ctx, *, session_id, start_round=0, memory_ctx=None):
        yield "data: [DONE]\n\n"

    answer = {"tool_use_id": "t1", "content": "[User approved] done"}

    # Resume pauses AGAIN: still no hooks, and the re-stash carries the
    # memory_ctx forward for the next resume.
    _stash("r-pause")
    monkeypatch.setattr(STREAM, "_tool_loop", _fake_repause)
    _drain(lambda: STREAM.resume_local_chat("r-pause", answer))
    assert calls == [], "memory hooks must not fire when the resume pauses again"
    stash = STREAM._local_paused.pop("r-pause")
    assert stash["memory_ctx"] == {"messages": msgs, "ws_path": "/ws"}

    # Resume completes: the hooks fire with the context stashed at pause time.
    _stash("r-done")
    monkeypatch.setattr(STREAM, "_tool_loop", _fake_done)
    _drain(lambda: STREAM.resume_local_chat("r-done", answer))
    assert calls == [("local_gemma", msgs, "r-done", "/ws")]

    # A pre-fix stash without memory_ctx (e.g. across a deploy) resumes cleanly
    # and just skips the hooks.
    _stash("r-legacy")
    del STREAM._local_paused["r-legacy"]["memory_ctx"]
    calls.clear()
    _drain(lambda: STREAM.resume_local_chat("r-legacy", answer))
    assert calls == []


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
