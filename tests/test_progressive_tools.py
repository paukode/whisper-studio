"""Progressive tool disclosure: partition, activation, index, caching, search."""

import json

import pytest

from server.chat import tool_activation
from server.chat.tool_index import build_deferred_index, estimate_tool_tokens
from server.chat.tool_partition import CORE_TOOLS, core_names, partition_pool


@pytest.fixture(autouse=True)
def clean_activation():
    with tool_activation._lock:
        tool_activation._activated.clear()
        tool_activation._versions.clear()
    yield


def _tool(name, desc="does things. and more."):
    return {"name": name, "description": desc, "input_schema": {"type": "object"}}


def _catalog():
    return [
        _tool("ws_read_file"),
        _tool("tool_search"),
        _tool("cron_create"),
        _tool("lsp_hover"),
        _tool("notebook_edit"),
    ]


# ── partition ────────────────────────────────────────────────────────────────


def test_partition_core_vs_deferred():
    advertised, deferred, core_count = partition_pool(_catalog(), [])
    assert [t["name"] for t in advertised] == ["ws_read_file", "tool_search"]
    assert core_count == 2
    assert {t["name"] for t in deferred} == {"cron_create", "lsp_hover", "notebook_edit"}


def test_partition_appends_activated_in_order_after_core():
    advertised, deferred, core_count = partition_pool(_catalog(), ["notebook_edit", "cron_create"])
    assert [t["name"] for t in advertised] == [
        "ws_read_file",
        "tool_search",
        "notebook_edit",
        "cron_create",
    ]
    assert core_count == 2
    assert [t["name"] for t in deferred] == ["lsp_hover"]


def test_partition_activated_respects_mode_filters():
    """An activated tool absent from the post-filter catalog (mode-blocked)
    must NOT be advertised — activation cannot bypass plan-mode blocks."""
    catalog = [t for t in _catalog() if t["name"] != "notebook_edit"]
    advertised, _, _ = partition_pool(catalog, ["notebook_edit"])
    assert "notebook_edit" not in {t["name"] for t in advertised}


def test_core_names_config_overrides(monkeypatch):
    from server.infrastructure import config as config_mod

    real = config_mod.load_config

    def _patched():
        cfg = dict(real())
        cfg["progressive_tools_core_extra"] = ["lsp_hover"]
        cfg["progressive_tools_defer_extra"] = ["ws_read_file", "tool_search"]
        return cfg

    monkeypatch.setattr(config_mod, "load_config", _patched)
    names = core_names()
    assert "lsp_hover" in names
    assert "ws_read_file" not in names
    assert "tool_search" in names  # never deferrable, even via config


# ── activation registry ──────────────────────────────────────────────────────


def test_activation_order_version_and_dedup():
    assert tool_activation.activate("s1", ["a", "b"]) == ["a", "b"]
    assert tool_activation.version("s1") == 1
    assert tool_activation.activate("s1", ["b", "c"]) == ["c"]
    assert tool_activation.get_ordered("s1") == ["a", "b", "c"]
    assert tool_activation.version("s1") == 2
    assert tool_activation.activate("s1", ["a"]) == []
    assert tool_activation.version("s1") == 2  # no change, no bump


def test_activate_from_history_first_seen_order():
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "sure"},
                {"type": "tool_use", "id": "1", "name": "lsp_hover", "input": {}},
                {"type": "tool_use", "id": "2", "name": "cron_create", "input": {}},
            ],
        },
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "3", "name": "lsp_hover", "input": {}}],
        },
    ]
    added = tool_activation.activate_from_history("s2", messages)
    assert added == ["lsp_hover", "cron_create"]


# ── index ────────────────────────────────────────────────────────────────────


def test_index_deterministic_and_instructive():
    _, deferred, _ = partition_pool(_catalog(), [])
    idx = build_deferred_index(deferred)
    assert "Additional tools (not loaded)" in idx
    assert "tool_search" in idx  # the instruction mentions the way back
    lines = [ln for ln in idx.split("\n") if ln.startswith("- ")]
    assert lines == sorted(lines)
    assert build_deferred_index([]) == ""
    assert estimate_tool_tokens(deferred) > 0


# ── caching breakpoints ──────────────────────────────────────────────────────


def test_core_count_breakpoint_added():
    from server.chat.caching import cached_tools_and_system

    tools = [_tool(f"t{i}") for i in range(5)]
    tools_field, _ = cached_tools_and_system(tools, "static", "", "1h", core_count=3)
    marked = [i for i, t in enumerate(tools_field) if "cache_control" in t]
    assert marked == [2, 4]  # core boundary + last tool
    # No core_count: single breakpoint on the last tool (legacy behavior).
    tools_field2, _ = cached_tools_and_system(tools, "static", "", "1h")
    assert [i for i, t in enumerate(tools_field2) if "cache_control" in t] == [4]
    # Degenerate: no appended tools -> no extra breakpoint.
    tools_field3, _ = cached_tools_and_system(tools, "static", "", "1h", core_count=5)
    assert [i for i, t in enumerate(tools_field3) if "cache_control" in t] == [4]


# ── pool assembly parity ─────────────────────────────────────────────────────


def test_legacy_pool_byte_identical_and_partitioned_smaller():
    from server.chat.tool_pool import (
        assemble_full_catalog,
        assemble_partitioned_pool,
        assemble_tool_pool,
    )

    full = assemble_full_catalog(ws_connected=False)
    legacy = assemble_tool_pool(ws_connected=False)
    assert [t["name"] for t in legacy] == [t["name"] for t in full]

    advertised, deferred, core_count = assemble_partitioned_pool(
        ws_connected=False, session_id="s-parity"
    )
    assert len(advertised) + len(deferred) == len(full)
    assert 0 < core_count <= len(advertised)
    assert len(advertised) < len(full)  # something actually deferred
    names = {t["name"] for t in advertised}
    assert "tool_search" in names


def test_progressive_pool_includes_activated():
    from server.chat.tool_pool import assemble_full_catalog, assemble_tool_pool

    full_names = {t["name"] for t in assemble_full_catalog(ws_connected=False)}
    target = next(iter(full_names - core_names()))
    tool_activation.activate("s-act", [target])
    pool = assemble_tool_pool(ws_connected=False, session_id="s-act", progressive=True)
    assert target in {t["name"] for t in pool}


# ── tool_search ──────────────────────────────────────────────────────────────


def test_tool_search_select_form_activates_and_returns_schema():
    from server.agent_tools import execute_tool_search

    out = json.loads(
        execute_tool_search({"query": "select:cron_create,lsp_hover"}, _catalog(), session_id="s3")
    )
    assert [m["name"] for m in out["matches"]] == ["cron_create", "lsp_hover"]
    assert all("input_schema" in m for m in out["matches"])
    assert out["activated"] == ["cron_create", "lsp_hover"]
    assert tool_activation.get_ordered("s3") == ["cron_create", "lsp_hover"]


def test_tool_search_keyword_activation_and_opt_out():
    from server.agent_tools import execute_tool_search

    out = json.loads(
        execute_tool_search({"query": "notebook", "activate": False}, _catalog(), session_id="s4")
    )
    assert [m["name"] for m in out["matches"]] == ["notebook_edit"]
    assert "activated" not in out
    assert tool_activation.get_ordered("s4") == []


def test_tool_search_activation_batch_cap():
    from server.agent_tools import _ACTIVATE_BATCH_MAX, execute_tool_search

    catalog = [_tool(f"widget_{i}", "widget maker") for i in range(10)]
    out = json.loads(
        execute_tool_search({"query": "widget", "max_results": 10}, catalog, session_id="s5")
    )
    assert len(out["activated"]) == _ACTIVATE_BATCH_MAX


def test_core_set_contains_the_essentials():
    assert {"tool_search", "skill_invoke", "spawn_agent", "team_create"} <= CORE_TOOLS
