"""Low-severity memory fixes.

(3) MEMORY.md byte-limit truncation cuts on ENCODED BYTES, not characters, so
    multibyte content can no longer exceed the byte cap or split a codepoint.
(4) Dream consolidation and the session summariser get their own agent types
    with dedicated system prompts (not the extraction prompt), and the summariser
    is read-only (no write tools).
"""

import asyncio

import server.memory.dream as dream
import server.memory.memdir as MD
from server.agents.config import AGENT_TYPES, WRITE_TOOLS, filter_tools_for_agent
from server.memory.prompts import (
    CONSOLIDATION_PROMPT,
    CONSOLIDATION_SYSTEM_PROMPT,
    EXTRACTION_SYSTEM_PROMPT,
    SESSION_SUMMARY_PROMPT,
)

# ── Fix (3): byte-accurate MEMORY.md truncation ──────────────────────────────


def test_memory_index_byte_truncation_is_byte_accurate(tmp_path, monkeypatch):
    """Over-limit multibyte MEMORY.md is truncated to <= the byte cap and stays
    valid UTF-8 (no partial codepoint at the cut)."""
    monkeypatch.setattr(MD, "MAX_ENTRYPOINT_BYTES", 100)
    monkeypatch.setattr(MD, "MAX_ENTRYPOINT_LINES", 10_000)  # isolate the byte path

    # 3-byte chars ('…' = U+2026). 100 is not a multiple of 3, so a byte-slice at
    # the cap lands mid-codepoint unless decoded with errors="ignore".
    line = "…" * 20  # 60 bytes
    text = "\n".join([line] * 10)  # ~610 bytes over 10 lines
    (tmp_path / MD.ENTRYPOINT_NAME).write_text(text, encoding="utf-8")

    out = MD.load_memory_index(str(tmp_path))
    body = out.split("\n\n> WARNING")[0]  # drop the appended truncation notice

    assert len(body.encode("utf-8")) <= MD.MAX_ENTRYPOINT_BYTES
    assert "�" not in body  # no replacement char / broken codepoint
    body.encode("utf-8").decode("utf-8")  # round-trips as valid UTF-8
    assert out != text  # it really was truncated
    assert "WARNING" in out  # and flagged as truncated


def test_memory_index_untruncated_when_small(tmp_path):
    """Small content passes through unchanged (no false truncation)."""
    text = "# Memory index\n\n- [x] a small note"
    (tmp_path / MD.ENTRYPOINT_NAME).write_text(text, encoding="utf-8")
    assert MD.load_memory_index(str(tmp_path)) == text


# ── Fix (4): dedicated agent types for consolidation / summary ───────────────


def test_consolidator_uses_dedicated_prompt_not_extraction():
    cfg = AGENT_TYPES["memory_consolidator"]
    assert cfg.system_prompt == CONSOLIDATION_SYSTEM_PROMPT
    assert cfg.system_prompt != EXTRACTION_SYSTEM_PROMPT
    assert "consolidation" in cfg.system_prompt.lower()


def test_summarizer_uses_summary_prompt_and_has_no_write_tools():
    cfg = AGENT_TYPES["session_summarizer"]
    assert cfg.system_prompt == SESSION_SUMMARY_PROMPT
    assert cfg.system_prompt != EXTRACTION_SYSTEM_PROMPT

    # The whitelist itself contains no write tool.
    assert cfg.allowed_tools is not None
    assert WRITE_TOOLS.isdisjoint(cfg.allowed_tools)
    assert "memory_write" not in cfg.allowed_tools
    assert "memory_delete" not in cfg.allowed_tools

    # ...and filtering a pool that offers writes strips them, keeping reads.
    pool = [
        {"name": n}
        for n in ("memory_read", "memory_list", "memory_write", "memory_delete", "ws_read_file")
    ]
    out = {t["name"] for t in filter_tools_for_agent(pool, cfg)}
    assert "memory_write" not in out and "memory_delete" not in out
    assert {"memory_read", "memory_list", "ws_read_file"} <= out


def test_dream_consolidate_runs_under_consolidator_agent(tmp_path, monkeypatch):
    """dream_consolidate dispatches to memory_consolidator (not memory_extractor),
    passing the scoped, phased consolidation plan as the task."""
    import server.agents.runtime as runtime

    captured: dict = {}

    class _Res:
        status = "completed"
        output = "done"
        turns_used = 1

    async def _fake_run_agent(task, **kwargs):
        captured["task"] = task
        captured["agent_type"] = kwargs.get("agent_type")
        return _Res()

    # dream_consolidate imports run_agent lazily from server.agents.runtime.
    monkeypatch.setattr(runtime, "run_agent", _fake_run_agent)
    monkeypatch.setattr(dream, "should_consolidate", lambda md: True)

    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    asyncio.run(dream.dream_consolidate(str(mem_dir), scope="global", model_id="m"))

    assert captured["agent_type"] == "memory_consolidator"
    assert captured["agent_type"] != "memory_extractor"
    assert captured["task"] == CONSOLIDATION_PROMPT.format(scope="global")
