"""On-device (local mode) metadata, prompts, and tool declarations.

The model itself is served by llama-server (covered by
tests/test_llama_server_backend.py); these exercise the isolation seam, the
registry, the local system prompt, and the tool-schema surface, all without a
GGUF or a subprocess.
"""

import asyncio

import pytest

import server.local.runtime as L
from server.local import tools as T


@pytest.fixture(autouse=True)
def _reset_requested_n_ctx():
    """The requested context window is a module global that intentionally persists
    (so a lazy start keeps the user's chosen size). Reset it around every test so
    one test's explicit n_ctx can't leak into another's default-path assertion."""
    L._requested_n_ctx = None
    yield
    L._requested_n_ctx = None


def test_registry_detects_local_keys_only():
    assert L.is_local_model("local_gemma")
    assert not L.is_local_model("opus4.8")
    assert not L.is_local_model(None)
    assert L.gguf_path("local_gemma").endswith(".gguf")


def test_is_local_model_id_discriminates_on_sentinel_prefix():
    assert L.is_local_model_id("local:gemma-4-12b-it-qat-q4_0")
    assert not L.is_local_model_id("anthropic.claude-opus-4-8")
    assert not L.is_local_model_id(None)


# ── Local memory stays offline ───────────────────────────────────────────────


def test_select_memories_skips_cloud_for_local(monkeypatch):
    """With >MAX_SELECTIONS memory files, the cloud path would rank via Haiku.
    For a local model id it must NOT — recall stays fully offline."""
    import server.memory.recall as R

    class _F:
        def __init__(self, i):
            self.path = f"/mem/{i}.md"
            self.filename = f"{i}.md"
            self.mtime = float(i)

    async def _boom(*a, **k):
        raise AssertionError("Haiku side-query must not run for a local model")

    monkeypatch.setattr(R, "_query_selector", _boom)

    entries = [("global", _F(i)) for i in range(6)]
    out = asyncio.run(R._select_entries("q", entries, model_id="local:gemma-4-12b-it-qat-q4_0"))
    assert len(out) == R.MAX_SELECTIONS  # most-recent files, no ranking, no cloud


def test_session_memory_summarizes_on_device_for_local(monkeypatch, tmp_path):
    """A local turn auto-updates session memory through the on-device model
    (runtime.complete), not the cloud memory_extractor agent, and writes the file."""
    import server.infrastructure.feature_flags as FF
    import server.memory.session_memory as SM

    monkeypatch.setattr(FF, "is_enabled", lambda flag: flag == "session_memory")
    target = tmp_path / "sess.md"
    monkeypatch.setattr(SM, "get_session_memory_path", lambda sid: str(target))

    used = {"local": False}

    def fake_complete(key, system_prompt, user, max_tokens=1500):
        used["local"] = True
        assert key == "local_gemma"
        return "## Goals\n- ship the context slider\n## Decisions\n## Context\n## Blockers\n"

    monkeypatch.setattr(L, "complete", fake_complete)

    # > LOCAL_TOKEN_THRESHOLD_CHARS so the lean local cadence fires (no tool calls needed).
    msgs = [{"role": "user", "content": "x" * (SM.LOCAL_TOKEN_THRESHOLD_CHARS + 100)}]
    asyncio.run(
        SM.maybe_update_session_memory(
            messages=msgs,
            session_id="local-sum-test",
            model_id="local:gemma-4-12b-it-qat-q4_0",
        )
    )

    assert used["local"]  # summarised on-device
    assert "ship the context slider" in target.read_text()


def test_session_memory_below_local_threshold_does_nothing(monkeypatch, tmp_path):
    """Lean cadence: a small local turn does not trigger a summary (so we don't
    round-trip the model server after every short turn)."""
    import server.infrastructure.feature_flags as FF
    import server.memory.session_memory as SM

    monkeypatch.setattr(FF, "is_enabled", lambda flag: flag == "session_memory")
    monkeypatch.setattr(SM, "get_session_memory_path", lambda sid: str(tmp_path / "s.md"))

    def boom(*a, **k):
        raise AssertionError("should not summarise below the local threshold")

    monkeypatch.setattr(L, "complete", boom)

    msgs = [{"role": "user", "content": "tiny"}]
    asyncio.run(
        SM.maybe_update_session_memory(
            messages=msgs,
            session_id="local-sum-test-2",
            model_id="local:gemma-4-12b-it-qat-q4_0",
        )
    )  # no exception ⇒ complete() never called


def test_local_system_prompt_is_lean_and_keeps_context(monkeypatch):
    # Stub out PROMPT_RULES.md — it is user-editable, so asserting on the
    # prompt tail or length with it injected would break whenever it changes.
    from server.prompts import rules

    monkeypatch.setattr(rules, "load_prompt_rules", lambda: "")
    p = L.build_local_system_prompt("PROJECT MEMORY HERE", "recalled fact", "")
    assert "locally" in p.lower()
    assert "do not have access to tools" in p.lower()  # honest about no tools
    assert "PROJECT MEMORY HERE" in p and "recalled fact" in p
    assert len(p) < 1000  # far leaner than the tool-heavy cloud system prompt
    # Empty extras are skipped cleanly.
    assert L.build_local_system_prompt("", "", "").strip().endswith("claim to use them.")


def test_local_system_prompt_appends_prompt_rules(monkeypatch):
    from server.prompts import rules

    monkeypatch.setattr(rules, "load_prompt_rules", lambda: "No emoji.")
    p = L.build_local_system_prompt("", "", "")
    assert "## Output rules" in p
    assert p.endswith("No emoji.")


def test_local_system_prompt_tools_mode_tells_model_it_has_tools():
    p = L.build_local_system_prompt(tools=True).lower()
    # Must NOT carry the no-tools disclaimer that makes the model refuse...
    assert "do not have access to tools" not in p
    # ...and must tell the model it has tools without over-promising a specific
    # one (the scope decides which tools are actually declared).
    assert "tools" in p
    assert "do not claim you lack tools" in p


def test_local_system_prompt_notes_connected_workspace_when_tools_on(monkeypatch):
    from server.prompts import rules

    monkeypatch.setattr(rules, "load_prompt_rules", lambda: "")
    p = L.build_local_system_prompt(tools=True, ws_path="/Users/me/proj")
    # The model must be told a workspace is connected at the path so it reaches
    # it via ws_* tools instead of claiming no project was provided.
    assert "/Users/me/proj" in p
    assert "ws_read_file" in p


def test_local_system_prompt_skips_workspace_note_without_tools(monkeypatch):
    from server.prompts import rules

    monkeypatch.setattr(rules, "load_prompt_rules", lambda: "")
    # No tools means no ws_* tools to call — the marker would over-promise, so
    # the honest no-tools identity stands even with a workspace connected.
    p = L.build_local_system_prompt(tools=False, ws_path="/Users/me/proj")
    assert "/Users/me/proj" not in p
    assert "do not have access to tools" in p.lower()


# ── Full tool parity: schema conversion + DSL parsing ────────────────────────


# ── Per-turn context-size threading (chip → llama-server) ────────────────────


def _run_local_chat(monkeypatch, body):
    """Call local_chat_response with a captured llama_server.ensure_serving,
    returning {n_ctx, model_key} (the engine turn is cut short by raising from
    the fake server start — n_ctx threading is what these tests pin)."""
    import asyncio

    from server.local import llama_server
    from server.local import route as R

    captured = {}

    def fake_ensure_serving(model_key, n_ctx=None):
        captured.update(n_ctx=n_ctx, model_key=model_key)
        raise RuntimeError("stop here: n_ctx captured")

    monkeypatch.setattr(llama_server, "ensure_serving", fake_ensure_serving)
    resp = R.local_chat_response(
        model_key="local_gemma",
        body=body,
        messages=[{"role": "user", "content": "hi"}],
        session_id="s1",
        approved_tool_result=None,
        transcript="",
        whisper_md_context="",
        memory_context="",
        session_memory_context="",
        plan_mode=False,
        mode="default",
        ws_path=None,
        session_approvals={},
        session_denials={},
        session_config={},
    )
    assert resp is not None

    async def drain():
        return [c async for c in resp.body_iterator]

    asyncio.run(drain())
    return captured


def test_chat_turn_threads_context_window_to_server(monkeypatch):
    """The composer chip value rides on every turn: a lazy llama-server start
    must use it, never a stale in-memory or registry default (the 64K-chip /
    n_ctx-32768 exceed_context_size mismatch)."""
    captured = _run_local_chat(monkeypatch, {"local_context_window": 65536})
    assert captured["n_ctx"] == 65536
    # Recorded for the non-chat callers (summariser, index extraction) too.
    assert L.requested_n_ctx() == 65536


def test_chat_turn_clamps_and_survives_garbage_context(monkeypatch):
    captured = _run_local_chat(monkeypatch, {"local_context_window": 10**9})
    assert captured["n_ctx"] == 262144  # clamped to Gemma's maximum

    captured = _run_local_chat(monkeypatch, {"local_context_window": "garbage"})
    assert captured["n_ctx"] is None  # unparsable → default tiers

    captured = _run_local_chat(monkeypatch, {})
    assert captured["n_ctx"] is None  # absent → default tiers (API callers)
