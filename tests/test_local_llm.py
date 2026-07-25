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
            # The shape llama-server's parsed calls are normalized into.
            [{"type": "tool_use", "id": "call_1", "name": "web_search", "input": {"query": "x"}}],
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
