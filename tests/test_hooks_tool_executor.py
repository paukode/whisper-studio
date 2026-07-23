"""WS-I wiring: execute_tool_batch honors PreToolUse deny/rewrite and folds
PostToolUse additionalContext back into the tool result the model reads.

route_tool is stubbed to an echo so these tests exercise the executor's hook
plumbing, not any real tool handler. Hooks are driven through the in-process
registry (the same path plugins and WS-E use).
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from server import tool_executor as te
from server.infrastructure import plugin_hooks


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "data"))
    saved = dict(plugin_hooks._hooks)
    plugin_hooks._hooks.clear()
    yield
    plugin_hooks._hooks.clear()
    plugin_hooks._hooks.update(saved)


def _batch(tool_name="ws_write_file", tool_input=None, monkeypatch=None, route=None):
    async def default_route(name, call_input, **kw):
        return (f"ran {name} with {call_input.get('path', '?')}", [])

    monkeypatch.setattr(te, "route_tool", route or default_route)
    executor = ThreadPoolExecutor(max_workers=2)

    async def _go():
        return await te.execute_tool_batch(
            [{"id": "t1", "name": tool_name, "input": tool_input or {"path": "/orig.txt"}}],
            is_concurrent_safe=lambda n: False,
            loop=asyncio.get_running_loop(),
            executor=executor,
            transcript="",
            attachments=None,
            session_id="s1",
            session_denials={},
            model_id="claude",
            plan_mode=False,
        )

    try:
        states = asyncio.run(_go())
    finally:
        executor.shutdown(wait=False)
    return states[0]


def test_pretooluse_deny_skips_tool(monkeypatch):
    async def deny(payload):
        return {"decision": "deny", "reason": "writes are frozen"}

    plugin_hooks.register_hook("PreToolUse", deny)
    st = _batch(monkeypatch=monkeypatch)
    assert st.status == "skipped"
    assert "writes are frozen" in st.output
    assert any("hook_blocked" in se for se in st.side_effects)


def test_pretooluse_security_deny_keeps_security_frame(monkeypatch):
    async def legacy(tool_name, tool_input):
        return {"reason": "secret in path", "findings": [{"rule": "no-secrets"}]}

    plugin_hooks.register_pre_tool_hook(legacy)
    st = _batch(monkeypatch=monkeypatch)
    assert st.status == "skipped"
    # Findings present → the existing security_blocked UI frame, not hook_blocked.
    assert any("security_blocked" in se for se in st.side_effects)
    assert not any("hook_blocked" in se for se in st.side_effects)


def test_pretooluse_rewrite_changes_input(monkeypatch):
    async def rewrite(payload):
        return {"decision": "rewrite", "updatedInput": {"path": "/safe.txt"}}

    plugin_hooks.register_hook("PreToolUse", rewrite)
    st = _batch(monkeypatch=monkeypatch)
    assert st.status == "completed"
    # route_tool saw the rewritten path.
    assert "/safe.txt" in st.output


def test_posttooluse_context_appended(monkeypatch):
    async def ctx(payload):
        return {"additionalContext": "note: file now exceeds 500 lines"}

    plugin_hooks.register_hook("PostToolUse", ctx)
    st = _batch(monkeypatch=monkeypatch)
    assert st.status == "completed"
    assert "exceeds 500 lines" in st.output
    assert "[Hook]" in st.output


def test_posttoolusefailure_context_appended(monkeypatch):
    async def boom(name, call_input, **kw):
        raise RuntimeError("disk full")

    async def ctx(payload):
        return {"additionalContext": "retry on a smaller batch"}

    plugin_hooks.register_hook("PostToolUseFailure", ctx)
    st = _batch(monkeypatch=monkeypatch, route=boom)
    assert st.status == "completed"
    assert "[Tool Error]" in st.output
    assert "retry on a smaller batch" in st.output


def test_no_hooks_is_transparent(monkeypatch):
    st = _batch(monkeypatch=monkeypatch)
    assert st.status == "completed"
    assert st.output == "ran ws_write_file with /orig.txt"
    assert st.side_effects == []
