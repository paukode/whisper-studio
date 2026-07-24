"""On-device (local mode) LLM runtime + SSE adapter.

These exercise the isolation seam and the streaming contract with the llama.cpp
runtime mocked, so they run without the GGUF or llama-cpp-python installed.
"""

import asyncio
import sys
import types

import pytest

import server.local.runtime as L
from server.local import tools as T
from server.local.stream import stream_local_chat


@pytest.fixture(autouse=True)
def _reset_requested_n_ctx():
    """The resident context window is a module global that intentionally persists
    across loads (so a lazy chat-turn load keeps the user's chosen size). Reset it
    around every test so one test's explicit n_ctx can't leak into another's
    default-path assertion."""
    L._requested_n_ctx = None
    yield
    L._requested_n_ctx = None


def _drain(agen_factory):
    async def run():
        return [c async for c in agen_factory()]

    return asyncio.run(run())


def test_registry_detects_local_keys_only():
    assert L.is_local_model("local_gemma")
    assert not L.is_local_model("opus4.8")
    assert not L.is_local_model(None)
    assert L.gguf_path("local_gemma").endswith(".gguf")


def test_is_local_model_id_discriminates_on_sentinel_prefix():
    assert L.is_local_model_id("local:gemma-4-12b-it-qat-q4_0")
    assert not L.is_local_model_id("anthropic.claude-opus-4-8")
    assert not L.is_local_model_id(None)


def test_load_sync_reloads_only_on_ctx_change(monkeypatch):
    """The load-bearing reload guard: same model + same n_ctx is a no-op; a
    different n_ctx forces a full reload (llama.cpp can't resize in place)."""
    created: list[int] = []

    class FakeLlama:
        def __init__(self, **kw):
            created.append(kw["n_ctx"])

    fake_mod = types.ModuleType("llama_cpp")
    fake_mod.Llama = FakeLlama
    monkeypatch.setitem(sys.modules, "llama_cpp", fake_mod)
    monkeypatch.setattr(L, "ensure_downloaded", lambda key: "/tmp/fake.gguf")
    monkeypatch.setattr(L, "_llm", None, raising=False)
    monkeypatch.setattr(L, "_llm_key", None, raising=False)

    L.load_sync("local_gemma", 16384)
    L.load_sync("local_gemma", 16384)  # same ctx → no reload
    assert created == [16384]
    assert L.is_loaded("local_gemma")

    L.load_sync("local_gemma", 32768)  # changed ctx → reload
    assert created == [16384, 32768]


def test_load_sync_default_ctx_is_16k(monkeypatch):
    """With no explicit n_ctx (and no env override), the model loads at the 16K
    default — the floor, since the tools-on prompt is ~12K tokens. The UI slider
    raises it on demand."""
    created: list[int] = []

    class FakeLlama:
        def __init__(self, **kw):
            created.append(kw["n_ctx"])

    fake_mod = types.ModuleType("llama_cpp")
    fake_mod.Llama = FakeLlama
    monkeypatch.setitem(sys.modules, "llama_cpp", fake_mod)
    monkeypatch.setattr(L, "ensure_downloaded", lambda key: "/tmp/fake.gguf")
    monkeypatch.setattr(L, "_llm", None, raising=False)
    monkeypatch.setattr(L, "_llm_key", None, raising=False)
    monkeypatch.delenv("WHISPER_LOCAL_N_CTX", raising=False)

    assert L.LOCAL_MODELS["local_gemma"]["ctx"] == 16384
    L.load_sync("local_gemma")  # no explicit n_ctx
    assert created == [16384]


def test_load_sync_keeps_requested_ctx_on_lazy_load(monkeypatch):
    """Regression: once the slider loads the model at a larger window, a lazy
    chat-turn load (``load_sync`` with no n_ctx) must KEEP that window, not fall
    back to the 16K default and reload the model smaller mid-session — which made
    the next message overflow a 16K window even though the badge said 32K/64K."""
    created: list[int] = []

    class FakeLlama:
        def __init__(self, **kw):
            created.append(kw["n_ctx"])

    fake_mod = types.ModuleType("llama_cpp")
    fake_mod.Llama = FakeLlama
    monkeypatch.setitem(sys.modules, "llama_cpp", fake_mod)
    monkeypatch.setattr(L, "ensure_downloaded", lambda key: "/tmp/fake.gguf")
    monkeypatch.setattr(L, "_llm", None, raising=False)
    monkeypatch.setattr(L, "_llm_key", None, raising=False)
    monkeypatch.delenv("WHISPER_LOCAL_N_CTX", raising=False)

    L.load_sync("local_gemma", 65536)  # user picks 64K via the slider
    L.load_sync("local_gemma")  # a chat turn lazily ensures residency
    # Only one construction: the 64K model stays resident; it is NOT reloaded at
    # the 16K default just because the lazy load passed no n_ctx.
    assert created == [65536]


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
    """A local turn auto-updates session memory via the on-device model
    (generate_round), not the cloud memory_extractor agent, and writes the file."""
    import server.infrastructure.feature_flags as FF
    import server.memory.session_memory as SM

    monkeypatch.setattr(FF, "is_enabled", lambda flag: flag == "session_memory")
    target = tmp_path / "sess.md"
    monkeypatch.setattr(SM, "get_session_memory_path", lambda sid: str(target))

    used = {"gemma": False}

    def fake_gen(key, convo, schemas, max_tokens=1024):
        used["gemma"] = True
        assert key == "local_gemma" and schemas == []
        return "## Goals\n- ship the context slider\n## Decisions\n## Context\n## Blockers\n"

    monkeypatch.setattr(L, "generate_round", fake_gen)

    # > LOCAL_TOKEN_THRESHOLD_CHARS so the lean local cadence fires (no tool calls needed).
    msgs = [{"role": "user", "content": "x" * (SM.LOCAL_TOKEN_THRESHOLD_CHARS + 100)}]
    asyncio.run(
        SM.maybe_update_session_memory(
            messages=msgs,
            session_id="local-sum-test",
            model_id="local:gemma-4-12b-it-qat-q4_0",
        )
    )

    assert used["gemma"]  # summarised on-device
    assert "ship the context slider" in target.read_text()


def test_session_memory_below_local_threshold_does_nothing(monkeypatch, tmp_path):
    """Lean cadence: a small local turn does not trigger a summary (so we don't
    summarise on the model thread after every short turn)."""
    import server.infrastructure.feature_flags as FF
    import server.memory.session_memory as SM

    monkeypatch.setattr(FF, "is_enabled", lambda flag: flag == "session_memory")
    monkeypatch.setattr(SM, "get_session_memory_path", lambda sid: str(tmp_path / "s.md"))

    def boom(*a, **k):
        raise AssertionError("should not summarise below the local threshold")

    monkeypatch.setattr(L, "generate_round", boom)

    msgs = [{"role": "user", "content": "tiny"}]
    asyncio.run(
        SM.maybe_update_session_memory(
            messages=msgs,
            session_id="local-sum-test-2",
            model_id="local:gemma-4-12b-it-qat-q4_0",
        )
    )  # no exception ⇒ generate_round never called


def test_to_chat_messages_flattens_and_drops_images():
    msgs = [
        {"role": "user", "content": [{"type": "image"}, {"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": "ok"},
    ]
    out = L._to_chat_messages("SYS", msgs)
    assert out[0] == {"role": "system", "content": "SYS"}
    assert {"role": "user", "content": "hi"} in out  # image block dropped
    assert {"role": "assistant", "content": "ok"} in out


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


def test_stream_emits_text_usage_then_done(monkeypatch):
    monkeypatch.setattr(
        L, "iter_chat", lambda *a, **k: iter([("text", "Hello"), ("text", " world")])
    )
    chunks = _drain(
        lambda: stream_local_chat("local_gemma", "sys", [{"role": "user", "content": "hi"}], "s")
    )
    blob = "".join(chunks)
    assert '"text": "Hello"' in blob and '"text": " world"' in blob
    assert '"usage"' in blob and '"estimated_cost_usd": 0.0' in blob
    assert chunks[-1].strip() == "data: [DONE]"


def test_stream_emits_thinking_events_when_thinking(monkeypatch):
    monkeypatch.setattr(
        L,
        "iter_chat",
        lambda *a, **k: iter([("thinking", "let me reason"), ("text", "the answer")]),
    )
    chunks = _drain(lambda: stream_local_chat("local_gemma", "s", [], "x", thinking=True))
    blob = "".join(chunks)
    assert '"thinking_start": true' in blob
    assert '"thinking": "let me reason"' in blob
    assert '"thinking_stop": true' in blob
    assert '"text": "the answer"' in blob
    # thinking_stop precedes the answer text
    assert blob.index('"thinking_stop"') < blob.index('"text": "the answer"')


def test_thought_splitter_separates_channel_from_answer():
    sp = L._ThoughtSplitter()
    out = []
    # Feed in chunks that split the markers across boundaries.
    for chunk in ["<|channel>thou", "ght\nreason", "ing here<chan", "nel|>final ", "answer"]:
        out.extend(sp.feed(chunk))
    out.extend(sp.flush())
    thinking = "".join(t for k, t in out if k == "thinking")
    text = "".join(t for k, t in out if k == "text")
    assert thinking == "reasoning here"
    assert text == "final answer"


def test_thought_splitter_all_text_when_no_markers():
    sp = L._ThoughtSplitter()
    out = []
    for chunk in ["just ", "a plain ", "answer"]:
        out.extend(sp.feed(chunk))
    out.extend(sp.flush())
    assert "".join(t for k, t in out if k == "thinking") == ""
    assert "".join(t for k, t in out if k == "text") == "just a plain answer"


def test_stream_surfaces_runtime_error_without_usage(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("llama-cpp-python is not installed")

    monkeypatch.setattr(L, "iter_chat", boom)
    chunks = _drain(lambda: stream_local_chat("local_gemma", "s", [], "x"))
    blob = "".join(chunks)
    assert '"error"' in blob and "llama-cpp-python" in blob
    assert '"usage"' not in blob  # no cost line on failure
    assert chunks[-1].strip() == "data: [DONE]"


# ── Full tool parity: schema conversion + DSL parsing ────────────────────────


def test_get_tool_schemas_exposes_full_pool_plus_web(monkeypatch):
    # memory_write only enters the pool when the auto_memory feature is on
    # (opt-in, ships off). Enable it so the "full pool" really is full — the
    # local model must see the same memory tools the cloud path does when the
    # user has the Memory toggle on. assemble_tool_pool re-imports is_enabled
    # per call, so patching the module attribute is honoured.
    import server.infrastructure.feature_flags as FF

    monkeypatch.setattr(FF, "is_enabled", lambda flag: flag == "auto_memory")
    schemas, names = T.get_tool_schemas(plan_mode=False, ws_connected=True, mcp_enabled_names=set())
    # web tools (not in assemble_tool_pool) are declared explicitly...
    assert "web_search" in names and "web_fetch" in names
    # ...alongside the full cloud pool (sampled).
    assert "memory_write" in names and "skill_invoke" in names
    assert len(schemas) > 10
    # Gemma's OpenAI function shape with JSON-schema parameters.
    s0 = schemas[0]
    assert s0["type"] == "function"
    assert set(s0["function"]) == {"name", "description", "parameters"}
    pool = next(s for s in schemas if s["function"]["name"] == "memory_write")
    assert pool["function"]["parameters"].get("type") == "object"


def test_get_tool_schemas_scopes_trim_the_pool(monkeypatch, tmp_path):
    """The scope filter trims how much of the pool the local model sees, so the
    user can trade capability for a smaller/faster prompt."""
    # Workspace + git tools only enter the pool when a connected workspace
    # resolves (and, for git, a .git dir). A bare test process has neither, so
    # point the lookup at a temp git repo. get_workspace_path is consulted via
    # two separate `from ... import` bindings — get_workspace_tools() (for the
    # ws_* tools) and assemble_tool_pool() (for the git check) — so both module
    # references must be patched, not just one.
    (tmp_path / ".git").mkdir()
    import server.chat.tool_pool as TP
    import server.workspace.tools as WT

    monkeypatch.setattr(WT, "get_workspace_path", lambda: str(tmp_path))
    monkeypatch.setattr(TP, "get_workspace_path", lambda: str(tmp_path))

    _, all_names = T.get_tool_schemas(ws_connected=True, scope="all")
    _, core_names = T.get_tool_schemas(ws_connected=True, scope="core")
    _, cw_names = T.get_tool_schemas(ws_connected=True, scope="core_web")

    # core: only the curated subset, and NO web tools.
    assert core_names <= T.CORE_TOOL_NAMES
    assert "ws_read_file" in core_names and "git_status" in core_names
    assert "web_search" not in core_names and "web_fetch" not in core_names
    assert "skill_invoke" not in core_names  # a non-core pool tool is excluded

    # core_web: the core subset PLUS the two web tools.
    assert cw_names - {"web_search", "web_fetch"} <= T.CORE_TOOL_NAMES
    assert "web_search" in cw_names and "web_fetch" in cw_names

    # all: the whole pool — much larger than core, and includes non-core tools.
    assert "skill_invoke" in all_names
    assert len(all_names) > len(core_names) + 5


def test_parse_tool_call_web_search():
    text = 'ok<|tool_call>call:web_search{query:<|"|>weather in Paris<|"|>}<tool_call|>'
    assert T.parse_tool_calls(text) == [("web_search", {"query": "weather in Paris"})]


def test_parse_tool_call_missing_close_marker():
    # We stop generation at <tool_call|>, so the close marker is often absent.
    text = '<|tool_call>call:web_fetch{url:<|"|>https://example.com<|"|>}'
    assert T.parse_tool_calls(text) == [("web_fetch", {"url": "https://example.com"})]


def test_parse_tool_call_coerces_scalars_and_json_body():
    # Bare scalars are coerced; a JSON-object body parses too.
    assert T.parse_tool_calls("<|tool_call>call:sleep{seconds:5}") == [("sleep", {"seconds": 5})]
    assert T.parse_tool_calls('<|tool_call>call:web_search{"query": "hi"}') == [
        ("web_search", {"query": "hi"})
    ]


def test_parse_tool_call_filters_unknown_when_names_given():
    # With a valid-name set, hallucinated tools are dropped; without it, anything
    # well-formed is returned (legacy behaviour).
    assert T.parse_tool_calls('<|tool_call>call:rm_rf{path:<|"|>/<|"|>}', {"web_search"}) == []
    assert T.parse_tool_calls('<|tool_call>call:rm_rf{path:<|"|>/<|"|>}') == [
        ("rm_rf", {"path": "/"})
    ]


def test_gemma_call_to_tool_use_synthesises_id():
    tu = T.gemma_call_to_tool_use("web_search", {"query": "x"})
    assert tu["type"] == "tool_use" and tu["name"] == "web_search"
    assert tu["id"].startswith("call_") and tu["input"] == {"query": "x"}


def test_strip_tool_markers_removes_dsl():
    out = T.strip_tool_markers('a <|tool_call>call:web_search{query:<|"|>x<|"|>}<tool_call|> b')
    assert "<|tool_call>" not in out and "a" in out and "b" in out


def test_run_tool_round_routes_through_the_safety_gate(monkeypatch):
    """The load-bearing safety invariant: run_tool_round MUST call
    process_tool_results (the [WS_APPROVAL] gate) and surface its returns —
    never bypass it or read state.output directly."""
    import server.tool_executor as TE

    seen = {}

    async def fake_batch(tool_uses, **kw):
        seen["batch"] = (tool_uses, kw)
        return ["STATE"]

    async def fake_process(states, budget_fn, **kw):
        seen["process"] = (states, kw)
        return (
            [{"type": "tool_result", "tool_use_id": "call_1", "content": "RESULT"}],
            ['{"skill_result": "web_search", "output": "RESULT"}'],
            False,
            False,
        )

    monkeypatch.setattr(TE, "execute_tool_batch", fake_batch)
    monkeypatch.setattr(TE, "process_tool_results", fake_process)

    async def run():
        return await T.run_tool_round(
            [T.gemma_call_to_tool_use("web_search", {"query": "x"})],
            session_id="s",
            session_approvals={},
            session_denials={},
            config={},
        )

    results, sse_events, pending, question = asyncio.run(run())
    # It went through BOTH calls...
    assert "batch" in seen and "process" in seen
    # ...and process_tool_results got offline-safe args (no Bedrock model id).
    assert seen["process"][1]["model_id"] == ""
    assert seen["batch"][1]["model_id"] == ""
    assert results[0]["content"] == "RESULT" and pending is False
    assert any("skill_result" in e for e in sse_events)


def test_tool_call_splitter_streams_text_and_withholds_dsl():
    """The streaming splitter shows answer text token-by-token but never leaks
    the <|tool_call> DSL to the user — while keeping the full raw for parsing."""
    sp = L._ToolCallSplitter()
    out = []
    for chunk in ["Let me ", "check.<|tool_", "call>call:web_search{q}"]:
        out.extend(sp.feed(chunk))
    out.extend(sp.flush())
    assert "".join(out) == "Let me check."  # DSL withheld
    assert sp.raw == "Let me check.<|tool_call>call:web_search{q}"  # full raw kept


def test_tool_call_splitter_streams_all_when_no_call():
    sp = L._ToolCallSplitter()
    out = []
    for chunk in ["The answer ", "is 42."]:
        out.extend(sp.feed(chunk))
    out.extend(sp.flush())
    assert "".join(out) == "The answer is 42."
    assert sp.raw == "The answer is 42."


def _fake_rounds(*texts):
    """An iter_generate_round stand-in: for each round, streams the displayable
    text (the part before any tool-call DSL) then yields ('raw', full_text)."""
    box = {"i": 0}

    def gen(key, convo, schemas, max_tokens=4096, cancel=None):
        i = box["i"]
        box["i"] = min(i + 1, len(texts) - 1)
        t = texts[i]
        displayable = t.split("<|tool_call>")[0] if "<|tool_call>" in t else t
        if displayable:
            yield ("text", displayable)
        yield ("raw", t)

    return gen


def test_stream_tools_runs_tool_then_answers(monkeypatch):
    import server.local.stream as S

    monkeypatch.setattr(L, "supports_tools", lambda k: True)
    monkeypatch.setattr(S, "get_tool_schemas", lambda **k: ([], {"web_search"}))
    monkeypatch.setattr(
        L,
        "iter_generate_round",
        _fake_rounds('<|tool_call>call:web_search{query:<|"|>weather<|"|>}', "It is sunny."),
    )

    async def fake_round(tool_uses, **kw):
        return (
            [{"type": "tool_result", "tool_use_id": tool_uses[0]["id"], "content": "Sunny, 20C"}],
            ['{"skill_result": "web_search", "output": "Sunny, 20C"}'],
            False,
            False,
        )

    monkeypatch.setattr(S, "run_tool_round", fake_round)

    chunks = _drain(lambda: stream_local_chat("local_gemma", "s", [], "x", tools=True, tool_ctx={}))
    blob = "".join(chunks)
    assert '"skill": "web_search"' in blob
    assert '"skill_input": "web_search"' in blob and '"query": "weather"' in blob
    assert '"skill_result": "web_search"' in blob and "Sunny, 20C" in blob
    assert '"text": "It is sunny."' in blob
    assert '"usage"' in blob
    assert chunks[-1].strip() == "data: [DONE]"


def test_stream_tools_pauses_on_approval_and_resumes(monkeypatch):
    """A destructive tool needing approval pauses (real approval_request card +
    stashed state, NO answer), then resume_local_chat continues with the
    approved result."""
    import server.local.stream as S

    monkeypatch.setattr(L, "supports_tools", lambda k: True)
    monkeypatch.setattr(S, "get_tool_schemas", lambda **k: ([], {"ws_write_file"}))
    monkeypatch.setattr(
        L,
        "iter_generate_round",
        _fake_rounds('<|tool_call>call:ws_write_file{path:<|"|>a.txt<|"|>}', "Done. File written."),
    )

    async def fake_round(tool_uses, **kw):
        tid = tool_uses[0]["id"]
        return (
            [
                {
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": "[Not executed] awaiting approval",
                }
            ],
            [
                '{"approval_request": {"tool_use_id": "'
                + tid
                + '", "action": "file_write", "category": "write"}}'
            ],
            True,  # has_pending_approval
            False,
        )

    monkeypatch.setattr(S, "run_tool_round", fake_round)

    # Round 1: pause. The card is emitted, no answer, and state is stashed.
    chunks = _drain(
        lambda: stream_local_chat("local_gemma", "s", [], "sess", tools=True, tool_ctx={})
    )
    blob = "".join(chunks)
    assert '"approval_request"' in blob
    assert '"skill": "ws_write_file"' in blob
    assert '"text"' not in blob  # destructive action did NOT run / answer
    assert S.has_local_pause("sess")
    assert chunks[-1].strip() == "data: [DONE]"

    # Resume: the action already ran via /api/approval/execute; we inject its
    # result and the model answers. Pause state is consumed.
    tid = next(iter(S._local_paused["sess"]["names_by_id"]))
    resume_chunks = _drain(
        lambda: S.resume_local_chat(
            "sess",
            {"tool_use_id": tid, "content": "[User approved] file_write a.txt succeeded."},
            {},
        )
    )
    rblob = "".join(resume_chunks)
    assert '"skill_result": "ws_write_file"' in rblob and "approved" in rblob
    assert '"text": "Done. File written."' in rblob
    assert not S.has_local_pause("sess")
    assert resume_chunks[-1].strip() == "data: [DONE]"


def test_unload_closes_the_model_to_free_memory(monkeypatch):
    """Switching local models must close() the resident one so a second 12B is
    not loaded on top of the first — that double-allocation OOMs / freezes an
    18 GB machine. Relying on ``_llm = None`` + gc alone does not free the C /
    Metal weights when any reference lingers."""

    class _FakeLlama:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    fake = _FakeLlama()
    monkeypatch.setattr(L, "_llm", fake, raising=False)
    monkeypatch.setattr(L, "_llm_key", "local_gemma", raising=False)

    L.unload_sync()

    assert fake.closed is True  # weights/KV cache explicitly freed
    assert L._llm is None and L._llm_key is None


def test_local_summarize_transcript_gets_transcript_from_tool_ctx(monkeypatch):
    """Regression (PR #135 follow-up): the on-device path must thread the
    request transcript through ``tool_ctx`` into ``run_tool_round`` so the real
    ``summarize_transcript`` executor sees it. Before the fix, tool_ctx carried
    no transcript, ``run_tool_round`` defaulted it to "", and exec_summarize
    returned "No transcript available to summarize." for every local turn.

    Drives the FULL real pipeline (no run_tool_round mock) so the assertion
    covers the whole tool_ctx -> run_tool_round -> execute_tool_batch ->
    exec_summarize chain."""
    import server.executors.content  # noqa: F401 — registers summarize_transcript
    import server.local.stream as S
    import server.skills as sk

    sk.SKILLS = sk.load_skills()

    monkeypatch.setattr(L, "supports_tools", lambda k: True)
    monkeypatch.setattr(S, "get_tool_schemas", lambda **k: ([], {"summarize_transcript"}))
    monkeypatch.setattr(
        L,
        "iter_generate_round",
        _fake_rounds(
            '<|tool_call>call:summarize_transcript{style:<|"|>brief<|"|>}',
            "Here is the summary.",
        ),
    )

    transcript = "Alice: We ship on Friday. Bob: Agreed, code freeze is Thursday."
    chunks = _drain(
        lambda: stream_local_chat(
            "local_gemma", "sys", [], "sess-tx", tools=True, tool_ctx={"transcript": transcript}
        )
    )
    blob = "".join(chunks)
    assert "No transcript available" not in blob
    # The real transcript reached exec_summarize and came back in the result.
    assert "We ship on Friday" in blob
    assert chunks[-1].strip() == "data: [DONE]"
