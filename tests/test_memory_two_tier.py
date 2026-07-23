"""Two-tier memory: global (cross-workspace) + project (workspace-scoped).

Covers scope routing in the executors, tier-merged recall, extraction
without a workspace, secret scanning on the global tier, and the
path-traversal guard. Global memory must work with NO workspace open,
that was the original bug: the "Memory (global)" toggle promised
system-wide memory while every code path was gated on a workspace.
"""

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

import server.infrastructure.feature_flags as FF
import server.memory.executor as EX
import server.memory.extract as XT
import server.memory.memdir as MD
import server.memory.recall as RC


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
def no_workspace(monkeypatch):
    monkeypatch.setattr(EX, "get_workspace_path", lambda: None)


@pytest.fixture
def with_workspace(monkeypatch, tmp_path):
    ws = tmp_path / "myproject"
    ws.mkdir()
    monkeypatch.setattr(EX, "get_workspace_path", lambda: str(ws))
    return ws


def _write(filename, mem_type, scope="", content="fact", name="A fact"):
    tool_input = {
        "filename": filename,
        "name": name,
        "description": "a test fact",
        "type": mem_type,
        "content": content,
    }
    if scope:
        tool_input["scope"] = scope
    return EX.execute_memory_write(tool_input)


def _proj_dir(mem_dirs, ws):
    """Project-tier dir for a workspace, using the same disambiguated slug the
    production code computes (sanitized basename + realpath hash)."""
    return mem_dirs["project_base"] / MD.get_workspace_slug(str(ws))


# ── Write routing ────────────────────────────────────────────────────────────


def test_global_write_without_workspace(mem_dirs, no_workspace):
    """The original bug: no workspace meant no memory at all. Global writes
    must succeed in plain chat mode."""
    result = _write("user_prefs.md", "user")
    assert "[scope: global]" in result

    saved = mem_dirs["global"] / "user_prefs.md"
    assert saved.is_file()
    text = saved.read_text()
    assert text.startswith("---\nname: A fact\n")
    assert "fact" in text


def test_project_scope_without_workspace_errors(mem_dirs, no_workspace):
    result = _write("goals.md", "project", scope="project")
    assert result.startswith("Error:")
    assert "workspace" in result.lower()


def test_type_routing_with_workspace(mem_dirs, with_workspace):
    r1 = _write("user_prefs.md", "user")
    assert "[scope: global]" in r1
    assert (mem_dirs["global"] / "user_prefs.md").is_file()

    r2 = _write("goals.md", "project")
    assert "[scope: project]" in r2
    slug_dir = _proj_dir(mem_dirs, with_workspace)
    assert (slug_dir / "goals.md").is_file()


def test_project_type_falls_back_to_global_without_workspace(mem_dirs, no_workspace):
    result = _write("goals.md", "project")
    assert "[scope: global]" in result
    assert (mem_dirs["global"] / "goals.md").is_file()


def test_explicit_scope_overrides_type_default(mem_dirs, with_workspace):
    result = _write("user_prefs.md", "user", scope="project")
    assert "[scope: project]" in result
    assert (_proj_dir(mem_dirs, with_workspace) / "user_prefs.md").is_file()
    assert not (mem_dirs["global"] / "user_prefs.md").exists()


def test_invalid_scope_rejected(mem_dirs, with_workspace):
    result = _write("x.md", "user", scope="everywhere")
    assert result.startswith("Error:")
    assert "scope" in result


def test_no_tmp_files_left_behind(mem_dirs, no_workspace):
    _write("a.md", "user")
    leftovers = [f for f in os.listdir(mem_dirs["global"]) if f.startswith(".tmp-memory-")]
    assert leftovers == []


def test_concurrent_writes_all_land(mem_dirs, no_workspace):
    """The global tier is shared across sessions; parallel writes must not
    corrupt or drop files."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: _write(f"f{i}.md", "user", content=f"c{i}"), range(24)))
    assert all("[scope: global]" in r for r in results)
    for i in range(24):
        assert (mem_dirs["global"] / f"f{i}.md").read_text().endswith(f"c{i}")


# ── Read / delete search order ───────────────────────────────────────────────


def test_read_prefers_project_then_global(mem_dirs, with_workspace):
    _write("shared.md", "user", scope="global", content="global copy")
    _write("shared.md", "user", scope="project", content="project copy")

    out = EX.execute_memory_read({"filename": "shared.md"})
    assert out.startswith("[scope: project]")
    assert "project copy" in out

    out_g = EX.execute_memory_read({"filename": "shared.md", "scope": "global"})
    assert out_g.startswith("[scope: global]")
    assert "global copy" in out_g


def test_read_falls_back_to_global(mem_dirs, with_workspace):
    _write("only_global.md", "user", scope="global")
    out = EX.execute_memory_read({"filename": "only_global.md"})
    assert out.startswith("[scope: global]")


def test_read_not_found_names_searched_tiers(mem_dirs, no_workspace):
    out = EX.execute_memory_read({"filename": "nope.md"})
    assert "not found in global memory" in out


def test_delete_scoped(mem_dirs, with_workspace):
    _write("shared.md", "user", scope="global")
    _write("shared.md", "user", scope="project")

    r1 = EX.execute_memory_delete({"filename": "shared.md"})
    assert "[scope: project]" in r1
    assert (mem_dirs["global"] / "shared.md").is_file()

    r2 = EX.execute_memory_delete({"filename": "shared.md"})
    assert "[scope: global]" in r2
    assert not (mem_dirs["global"] / "shared.md").exists()


# ── List ─────────────────────────────────────────────────────────────────────


def test_list_shows_both_tiers(mem_dirs, with_workspace):
    _write("g.md", "user", scope="global")
    _write("p.md", "project", scope="project")
    out = EX.execute_memory_list({})
    assert "## Global memory (1 files)" in out
    assert "## Project memory (1 files)" in out
    assert "g.md" in out and "p.md" in out


def test_list_without_workspace_notes_missing_project_tier(mem_dirs, no_workspace):
    _write("g.md", "user")
    out = EX.execute_memory_list({})
    assert "## Global memory" in out
    assert "## Project memory" not in out
    assert "No workspace connected" in out


# ── Guards ───────────────────────────────────────────────────────────────────


def test_secret_scanner_blocks_global_write(mem_dirs, no_workspace):
    token = "ghp_" + "a1B2" * 9  # GitHub PAT shape: ghp_ + 36 alphanumerics
    result = _write("creds.md", "user", content=f"my token is {token}")
    assert result.startswith("BLOCKED")
    assert not (mem_dirs["global"] / "creds.md").exists()


def test_path_traversal_blocked_in_global(mem_dirs, no_workspace):
    result = _write("../../evil.md", "user")
    assert "traversal" in result.lower()
    assert not (mem_dirs["global"].parent / "evil.md").exists()


def test_memory_md_write_and_delete_blocked(mem_dirs, no_workspace):
    r = _write("MEMORY.md", "user")
    assert "Cannot write MEMORY.md" in r
    r2 = EX.execute_memory_delete({"filename": "MEMORY.md"})
    assert "Cannot delete MEMORY.md" in r2


def test_memory_md_guard_is_case_insensitive(mem_dirs, no_workspace):
    """APFS is case-insensitive: writing memory.md there would silently
    overwrite MEMORY.md, so cased variants must hit the same guard."""
    for variant in ("memory.md", "Memory.md", "sub/MEMORY.MD"):
        r = _write(variant, "user")
        assert "Cannot write MEMORY.md" in r, variant
    r2 = EX.execute_memory_delete({"filename": "memory.md"})
    assert "Cannot delete MEMORY.md" in r2


# ── Recall (tier merge) ──────────────────────────────────────────────────────


def _seed(mem_dirs, scope, filename, desc="d"):
    """Write a topic file directly into a tier directory."""
    if scope == "global":
        base = mem_dirs["global"]
    else:
        base = _proj_dir(mem_dirs, EX.get_workspace_path())
    base.mkdir(parents=True, exist_ok=True)
    (base / filename).write_text(
        f"---\nname: {filename}\ndescription: {desc}\ntype: user\n---\n\nbody of {filename}\n"
    )


def test_recall_merges_both_tiers(mem_dirs, with_workspace):
    _seed(mem_dirs, "global", "gfact.md")
    _seed(mem_dirs, "project", "pfact.md")

    ctx, _n = asyncio.run(
        RC.recall_memory_context("q", str(with_workspace), model_id="anthropic.claude-x")
    )
    assert "<memory-context>" in ctx
    assert "gfact.md (scope: global" in ctx
    assert "pfact.md (scope: project" in ctx
    assert "body of gfact.md" in ctx


def test_recall_global_only_without_workspace(mem_dirs):
    _seed(mem_dirs, "global", "gfact.md")
    ctx, _n = asyncio.run(RC.recall_memory_context("q", None, model_id="anthropic.claude-x"))
    assert "gfact.md (scope: global" in ctx
    assert "project" not in ctx


def test_recall_empty_when_flag_off(mem_dirs, monkeypatch):
    monkeypatch.setattr(FF, "is_enabled", lambda flag: False)
    _seed(mem_dirs, "global", "gfact.md")
    ctx, _n = asyncio.run(RC.recall_memory_context("q", None, model_id="anthropic.claude-x"))
    assert ctx == ""


def test_recall_selector_gets_tier_qualified_keys(mem_dirs, monkeypatch):
    """With >MAX_SELECTIONS files the Haiku selector runs; its manifest keys
    must be tier-qualified and its answers mapped back to real paths."""
    for i in range(7):
        _seed(mem_dirs, "global", f"f{i}.md")

    captured = {}

    async def fake_selector(query, manifest, model_id):
        captured["manifest"] = manifest
        return ["global/f0.md", "global/f3.md"]

    monkeypatch.setattr(RC, "_query_selector", fake_selector)
    ctx, _n = asyncio.run(RC.recall_memory_context("q", None, model_id="anthropic.claude-x"))

    assert "global/f0.md" in captured["manifest"]
    assert "global/f6.md" in captured["manifest"]
    assert "f0.md (scope: global" in ctx
    assert "f3.md (scope: global" in ctx
    assert "f1.md (scope: global" not in ctx


def test_recall_selector_bare_filename_fallback(mem_dirs, monkeypatch):
    """If the selector drops the tier prefix, recall still resolves the file."""
    for i in range(7):
        _seed(mem_dirs, "global", f"f{i}.md")

    async def fake_selector(query, manifest, model_id):
        return ["f2.md"]

    monkeypatch.setattr(RC, "_query_selector", fake_selector)
    ctx, _n = asyncio.run(RC.recall_memory_context("q", None, model_id="anthropic.claude-x"))
    assert "f2.md (scope: global" in ctx


# ── Extraction without a workspace ───────────────────────────────────────────


class _AgentResult:
    status = "completed"
    turns_used = 1
    tools_called = ["memory_write"]
    output = ""


@pytest.fixture
def fresh_extract_state():
    XT._cursors.clear()
    XT._turn_counters.clear()
    XT._inflight.clear()
    yield
    XT._cursors.clear()
    XT._turn_counters.clear()
    XT._inflight.clear()


def _messages(n):
    return [{"role": "user", "content": f"m{i}"} for i in range(n)]


def test_extraction_fires_without_workspace(mem_dirs, fresh_extract_state, monkeypatch):
    """auto_memory on + no workspace: extraction must still run (global tier),
    firing on the 3rd turn with global-only routing guidance."""
    import server.agents.runtime as RT

    calls = []

    async def fake_run_agent(task, **kwargs):
        calls.append({"task": task, **kwargs})
        return _AgentResult()

    monkeypatch.setattr(RT, "run_agent", fake_run_agent)

    for turn in range(3):
        asyncio.run(
            XT.maybe_extract_memory(
                messages=_messages(4 + turn),
                session_id="sess-1",
                ws_path=None,
                model_id="anthropic.claude-x",
            )
        )

    assert len(calls) == 1
    assert "No workspace is open" in calls[0]["task"]
    assert calls[0]["agent_type"] == "memory_extractor"

    # Cursor persisted in the global tier (the anchor dir without a workspace)
    cursor_file = mem_dirs["global"] / ".cursor.json"
    assert cursor_file.is_file()
    data = json.loads(cursor_file.read_text())
    assert data["sessions"]["sess-1"] == 6


def test_extraction_task_mentions_both_tiers_with_workspace(
    mem_dirs, fresh_extract_state, monkeypatch, tmp_path
):
    import server.agents.runtime as RT

    ws = tmp_path / "myproject"
    ws.mkdir(exist_ok=True)
    calls = []

    async def fake_run_agent(task, **kwargs):
        calls.append(task)
        return _AgentResult()

    monkeypatch.setattr(RT, "run_agent", fake_run_agent)

    for _turn in range(3):
        asyncio.run(
            XT.maybe_extract_memory(
                messages=_messages(4),
                session_id="sess-2",
                ws_path=str(ws),
                model_id="anthropic.claude-x",
            )
        )

    assert len(calls) == 1
    assert "Both memory tiers are available" in calls[0]
    assert "scope='global'" in calls[0]
    assert "scope='project'" in calls[0]


def test_extraction_turn_counters_are_per_session(mem_dirs, fresh_extract_state, monkeypatch):
    """Two interleaved sessions must not share the every-3rd-turn throttle."""
    import server.agents.runtime as RT

    calls = []

    async def fake_run_agent(task, **kwargs):
        calls.append(kwargs.get("session_id"))
        return _AgentResult()

    monkeypatch.setattr(RT, "run_agent", fake_run_agent)

    # Interleave: a,b,a,b,a  ->  session a hits 3 turns, b only 2
    for sid in ["a", "b", "a", "b", "a"]:
        asyncio.run(
            XT.maybe_extract_memory(
                messages=_messages(4),
                session_id=sid,
                ws_path=None,
                model_id="anthropic.claude-x",
            )
        )

    assert calls == ["a"]


def test_extraction_disabled_when_flag_off(fresh_extract_state, monkeypatch, tmp_path):
    import server.agents.runtime as RT

    monkeypatch.setattr(MD, "GLOBAL_MEMORY_DIR", str(tmp_path / "g"))
    monkeypatch.setattr(FF, "is_enabled", lambda flag: False)

    async def fake_run_agent(task, **kwargs):
        raise AssertionError("extraction must not run when auto_memory is off")

    monkeypatch.setattr(RT, "run_agent", fake_run_agent)

    for _ in range(3):
        asyncio.run(
            XT.maybe_extract_memory(
                messages=_messages(4),
                session_id="sess-3",
                ws_path=None,
                model_id="anthropic.claude-x",
            )
        )
    assert not (tmp_path / "g").exists()


# ── Review-driven regressions ────────────────────────────────────────────────


def test_unscoped_write_updates_existing_project_file_in_place(mem_dirs, with_workspace):
    """Legacy stores keep user/feedback files in the project tier. An unscoped
    update must land on the existing file, not fork a global copy that the
    project-first read order would then shadow with stale content."""
    _write("user_role.md", "user", scope="project", content="old role")

    result = _write("user_role.md", "user", content="new role")
    assert "[scope: project]" in result

    project_copy = _proj_dir(mem_dirs, with_workspace) / "user_role.md"
    assert "new role" in project_copy.read_text()
    assert not (mem_dirs["global"] / "user_role.md").exists()

    out = EX.execute_memory_read({"filename": "user_role.md"})
    assert "new role" in out


def test_recall_selector_nested_path_without_tier_prefix(mem_dirs, monkeypatch):
    """Nested filenames (aws/creds.md) must survive a selector that strips the
    tier prefix; the old fallback only rescued keys with no slash at all."""
    for i in range(6):
        _seed(mem_dirs, "global", f"f{i}.md")
    nested = mem_dirs["global"] / "aws"
    nested.mkdir()
    (nested / "creds.md").write_text(
        "---\nname: AWS\ndescription: aws pointers\ntype: reference\n---\n\naws body\n"
    )

    async def fake_selector(query, manifest, model_id):
        assert "global/aws/creds.md" in manifest
        return ["aws/creds.md"]  # tier prefix stripped by the model

    monkeypatch.setattr(RC, "_query_selector", fake_selector)
    ctx, _n = asyncio.run(RC.recall_memory_context("q", None, model_id="anthropic.claude-x"))
    assert "aws/creds.md (scope: global" in ctx
    assert "aws body" in ctx


def test_recall_selector_duplicate_keys_deduped(mem_dirs, monkeypatch):
    for i in range(6):
        _seed(mem_dirs, "global", f"f{i}.md")

    async def fake_selector(query, manifest, model_id):
        return ["global/f1.md", "f1.md", "global/f1.md"]

    monkeypatch.setattr(RC, "_query_selector", fake_selector)
    ctx, _n = asyncio.run(RC.recall_memory_context("q", None, model_id="anthropic.claude-x"))
    assert ctx.count("# Memory: f1.md") == 1


def test_cursor_survives_workspace_flip(mem_dirs, fresh_extract_state, monkeypatch, tmp_path):
    """The cursor anchor is always the global tier: a session that extracted in
    plain chat must not re-extract old messages after a workspace connects
    (and a server restart clears the in-memory cache)."""
    import server.agents.runtime as RT

    calls = []

    async def fake_run_agent(task, **kwargs):
        calls.append(task)
        return _AgentResult()

    monkeypatch.setattr(RT, "run_agent", fake_run_agent)

    msgs = _messages(6)
    for _ in range(3):
        asyncio.run(
            XT.maybe_extract_memory(messages=msgs, session_id="flip", ws_path=None, model_id="m")
        )
    assert len(calls) == 1

    # Simulate restart (in-memory cache gone) + workspace now connected
    XT._cursors.clear()
    ws = tmp_path / "myproject"
    ws.mkdir(exist_ok=True)
    for _ in range(3):
        asyncio.run(
            XT.maybe_extract_memory(messages=msgs, session_id="flip", ws_path=str(ws), model_id="m")
        )
    # Same message list, cursor recovered from the global tier: nothing new
    assert len(calls) == 1


def test_cursor_claimed_before_agent_runs(mem_dirs, fresh_extract_state, monkeypatch):
    """The cursor is claimed before the agent runs, so a failing run skips its
    slice instead of retrying it (and stacking duplicates)."""
    import server.agents.runtime as RT

    async def failing_run_agent(task, **kwargs):
        raise RuntimeError("agent died")

    monkeypatch.setattr(RT, "run_agent", failing_run_agent)

    for _ in range(3):
        asyncio.run(
            XT.maybe_extract_memory(
                messages=_messages(5), session_id="claim", ws_path=None, model_id="m"
            )
        )

    data = json.loads((mem_dirs["global"] / ".cursor.json").read_text())
    assert data["sessions"]["claim"] == 5
    assert "claim" not in XT._inflight  # released even on failure


def test_inflight_guard_skips_overlapping_extraction(mem_dirs, fresh_extract_state, monkeypatch):
    import server.agents.runtime as RT

    calls = []

    async def fake_run_agent(task, **kwargs):
        calls.append(task)
        return _AgentResult()

    monkeypatch.setattr(RT, "run_agent", fake_run_agent)
    XT._inflight.add("busy")

    for _ in range(3):
        asyncio.run(
            XT.maybe_extract_memory(
                messages=_messages(5), session_id="busy", ws_path=None, model_id="m"
            )
        )
    assert calls == []


def test_drop_session_clears_extraction_state(fresh_extract_state):
    XT._cursors["gone"] = 7
    XT._turn_counters["gone"] = 2
    XT._inflight.add("gone")
    XT.drop_session("gone")
    assert "gone" not in XT._cursors
    assert "gone" not in XT._turn_counters
    assert "gone" not in XT._inflight


# ── MEMORY.md auto-index (PR 2) ──────────────────────────────────────────────


def test_write_regenerates_index(mem_dirs, no_workspace):
    _write("user_prefs.md", "user", content="likes brevity")
    index = (mem_dirs["global"] / "MEMORY.md").read_text()
    assert "Auto-generated" in index
    assert "[user] [A fact](user_prefs.md): a test fact" in index

    _write("feedback_style.md", "feedback", name="Style")
    index = (mem_dirs["global"] / "MEMORY.md").read_text()
    assert "user_prefs.md" in index and "feedback_style.md" in index


def test_delete_regenerates_index(mem_dirs, no_workspace):
    _write("a.md", "user", name="A")
    _write("b.md", "user", name="B")
    EX.execute_memory_delete({"filename": "a.md"})
    index = (mem_dirs["global"] / "MEMORY.md").read_text()
    assert "a.md" not in index and "b.md" in index

    # Deleting the last topic file removes the index instead of leaving it stale
    EX.execute_memory_delete({"filename": "b.md"})
    assert not (mem_dirs["global"] / "MEMORY.md").exists()


def test_index_is_per_tier(mem_dirs, with_workspace):
    _write("g.md", "user", scope="global", name="G")
    _write("p.md", "project", scope="project", name="P")
    g_index = (mem_dirs["global"] / "MEMORY.md").read_text()
    p_index = (_proj_dir(mem_dirs, with_workspace) / "MEMORY.md").read_text()
    assert "g.md" in g_index and "p.md" not in g_index
    assert "p.md" in p_index and "g.md" not in p_index


def test_index_never_indexes_itself(mem_dirs, no_workspace):
    _write("a.md", "user", name="A")
    _write("b.md", "user", name="B")  # second rebuild happens with MEMORY.md present
    index = (mem_dirs["global"] / "MEMORY.md").read_text()
    assert "MEMORY.md" not in index


def test_index_truncates_long_descriptions(mem_dirs, no_workspace):
    long_desc = "x" * 300
    EX.execute_memory_write(
        {
            "filename": "long.md",
            "name": "Long",
            "description": long_desc,
            "type": "user",
            "content": "c",
        }
    )
    index = (mem_dirs["global"] / "MEMORY.md").read_text()
    line = next(ln for ln in index.splitlines() if "long.md" in ln)
    assert len(line) < 200
    assert "..." in line


def test_recall_includes_generated_index(mem_dirs, no_workspace):
    _write("user_prefs.md", "user")
    ctx, _n = asyncio.run(RC.recall_memory_context("q", None, model_id="anthropic.claude-x"))
    assert "# Memory Index (global MEMORY.md)" in ctx


# ── Dream consolidation wiring (PR 2) ────────────────────────────────────────


def test_dream_triggers_when_due(mem_dirs, monkeypatch, tmp_path):
    """record_and_maybe_dream must actually RUN consolidation when a tier is
    due. (Before PR 2, dream_consolidate had no caller at all.)"""
    import server.agents.runtime as RT
    import server.memory.dream as DR

    monkeypatch.setattr(FF, "is_enabled", lambda flag: True)

    ran = []

    async def fake_run_agent(task, **kwargs):
        ran.append(task)
        return _AgentResult()

    monkeypatch.setattr(RT, "run_agent", fake_run_agent)

    # Make the global tier due: last consolidated 25h ago, 5 sessions recorded
    gdir = str(mem_dirs["global"])
    os.makedirs(gdir, exist_ok=True)
    DR._save_meta(gdir, {"last_consolidated_at": 0, "session_count": 4})

    ws = tmp_path / "myproject"
    ws.mkdir(exist_ok=True)
    asyncio.run(DR.record_and_maybe_dream(str(ws), model_id="m"))

    assert len(ran) == 1
    assert "global" in ran[0]  # consolidation prompt pinned to the due tier
    # Meta reset after the run
    meta = DR._load_meta(gdir)
    assert meta["session_count"] == 0
    assert meta["last_consolidated_at"] > 0


def test_dream_not_run_below_threshold(mem_dirs, monkeypatch):
    import server.agents.runtime as RT
    import server.memory.dream as DR

    monkeypatch.setattr(FF, "is_enabled", lambda flag: True)

    async def boom(task, **kwargs):
        raise AssertionError("must not consolidate below thresholds")

    monkeypatch.setattr(RT, "run_agent", boom)

    asyncio.run(DR.record_and_maybe_dream(None, model_id="m"))
    meta = DR._load_meta(str(mem_dirs["global"]))
    assert meta["session_count"] == 1  # recorded, not consolidated


def test_dream_flag_off_records_nothing(mem_dirs, monkeypatch):
    import server.memory.dream as DR

    monkeypatch.setattr(FF, "is_enabled", lambda flag: flag == "auto_memory")
    asyncio.run(DR.record_and_maybe_dream(None, model_id="m"))
    assert not (mem_dirs["global"] / ".dream_meta.json").exists()


# ── /api/memory REST API (PR 3) ──────────────────────────────────────────────


@pytest.fixture
def api_client(mem_dirs, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import server.memory.router as MR

    monkeypatch.setattr(MR, "get_workspace_path", lambda: None)
    app = FastAPI()
    app.include_router(MR.memory_router)
    return TestClient(app)


@pytest.fixture
def api_client_ws(mem_dirs, monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import server.memory.router as MR

    ws = tmp_path / "myproject"
    ws.mkdir(exist_ok=True)
    monkeypatch.setattr(MR, "get_workspace_path", lambda: str(ws))
    app = FastAPI()
    app.include_router(MR.memory_router)
    return TestClient(app)


def test_api_list_global_only(api_client, mem_dirs, no_workspace):
    _write("user_prefs.md", "user")
    resp = api_client.get("/api/memory")
    assert resp.status_code == 200
    data = resp.json()
    assert data["workspace_connected"] is False
    assert len(data["tiers"]) == 1
    tier = data["tiers"][0]
    assert tier["scope"] == "global"
    assert tier["files"][0]["filename"] == "user_prefs.md"
    assert "user_prefs.md" in tier["index"]


def test_api_file_roundtrip(api_client):
    body = {
        "scope": "global",
        "filename": "note.md",
        "content": "---\nname: Note\ndescription: d\ntype: user\n---\n\nhello\n",
    }
    assert api_client.put("/api/memory/file", json=body).status_code == 200
    got = api_client.get("/api/memory/file", params={"scope": "global", "filename": "note.md"})
    assert got.status_code == 200
    assert "hello" in got.json()["content"]

    # Index rebuilt on API writes too
    listing = api_client.get("/api/memory").json()
    assert "note.md" in listing["tiers"][0]["index"]

    assert (
        api_client.delete(
            "/api/memory/file", params={"scope": "global", "filename": "note.md"}
        ).status_code
        == 200
    )
    assert (
        api_client.get(
            "/api/memory/file", params={"scope": "global", "filename": "note.md"}
        ).status_code
        == 404
    )


def test_api_guards(api_client):
    # traversal
    r = api_client.put(
        "/api/memory/file",
        json={"scope": "global", "filename": "../evil.md", "content": "x"},
    )
    assert r.status_code == 400
    # MEMORY.md immutable
    r = api_client.put(
        "/api/memory/file",
        json={"scope": "global", "filename": "memory.md", "content": "x"},
    )
    assert r.status_code == 400
    # secrets blocked
    token = "ghp_" + "a1B2" * 9
    r = api_client.put(
        "/api/memory/file",
        json={"scope": "global", "filename": "s.md", "content": f"tok {token}"},
    )
    assert r.status_code == 400
    # project scope without workspace
    r = api_client.get("/api/memory/file", params={"scope": "project", "filename": "x.md"})
    assert r.status_code == 400


def test_api_promote_moves_project_file_to_global(api_client_ws, mem_dirs, tmp_path):
    # api_client_ws points the router at tmp_path / "myproject"; resolve the
    # same disambiguated slug dir the endpoint will write to.
    src = _proj_dir(mem_dirs, tmp_path / "myproject")
    src.mkdir(parents=True, exist_ok=True)
    (src / "fact.md").write_text("---\nname: F\ndescription: d\ntype: user\n---\n\nbody\n")

    r = api_client_ws.post("/api/memory/promote", json={"filename": "fact.md"})
    assert r.status_code == 200
    assert not (src / "fact.md").exists()
    assert (mem_dirs["global"] / "fact.md").is_file()
    # Both indexes rebuilt
    assert "fact.md" in (mem_dirs["global"] / "MEMORY.md").read_text()
    assert not (src / "MEMORY.md").exists()  # empty project store: index removed

    # Second promote of same name: 404 (gone); existing-target conflict: 409
    (src / "fact.md").write_text("---\nname: F2\ndescription: d\ntype: user\n---\n\nv2\n")
    r = api_client_ws.post("/api/memory/promote", json={"filename": "fact.md"})
    assert r.status_code == 409


# ── Memory events (PR 3) ─────────────────────────────────────────────────────


def test_extraction_publishes_memory_event(
    mem_dirs, fresh_extract_state, no_workspace, monkeypatch
):
    """The event reports what actually changed on disk (snapshot diff), not
    attempted tool calls: a blocked or failed memory_write must not toast."""
    import server.agents.runtime as RT

    events = []

    class FakeBus:
        def publish(self, sid, ev):
            events.append((sid, ev))

    import server.agents.event_bus as EB

    monkeypatch.setattr(EB, "event_bus", FakeBus())

    # Pre-existing file the fake agent will delete
    _write("stale.md", "user", name="Stale")

    class _R:
        status = "completed"
        turns_used = 2
        tools_called = ["memory_list", "memory_write", "memory_write", "memory_delete"]
        output = ""

    async def fake_run_agent(task, **kwargs):
        # Really mutate the store, like the live agent's executors would:
        # two writes land, one delete lands, and one write is BLOCKED by the
        # secret scanner (must not be counted).
        _write("new_a.md", "user", name="A")
        _write("new_b.md", "user", name="B")
        _write("blocked.md", "user", name="Bad", content="tok ghp_" + "a1B2" * 9)
        EX.execute_memory_delete({"filename": "stale.md"})
        return _R()

    monkeypatch.setattr(RT, "run_agent", fake_run_agent)

    for _ in range(3):
        asyncio.run(
            XT.maybe_extract_memory(
                messages=_messages(4), session_id="ev-sess", ws_path=None, model_id="m"
            )
        )

    assert events == [
        (
            "ev-sess",
            {
                "type": "memory_event",
                "memoryEvent": {"action": "extracted", "writes": 2, "deletes": 1},
            },
        )
    ]


def test_extraction_no_event_when_nothing_written(mem_dirs, fresh_extract_state, monkeypatch):
    import server.agents.event_bus as EB
    import server.agents.runtime as RT

    events = []

    class FakeBus:
        def publish(self, sid, ev):
            events.append(ev)

    monkeypatch.setattr(EB, "event_bus", FakeBus())

    class _R:
        status = "completed"
        turns_used = 1
        tools_called = ["memory_list"]
        output = ""

    async def fake_run_agent(task, **kwargs):
        return _R()

    monkeypatch.setattr(RT, "run_agent", fake_run_agent)

    for _ in range(3):
        asyncio.run(
            XT.maybe_extract_memory(
                messages=_messages(4), session_id="ev-sess2", ws_path=None, model_id="m"
            )
        )
    assert events == []
