"""Verify oversize tool results are persisted to the result cache as
head+tail slices and emit a truncation event the frontend can render.
"""

import asyncio

import pytest

import server.executors.content  # noqa: F401 — registers the prompt executors
import server.skills as skills_mod
from server.chat import (
    TOOL_RESULT_BUDGET_BYTES,
    _budget_tool_result,
    make_budget_tool_result,
)
from server.skills import produces_model_prompt
from server.tool_executor import process_tool_results


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Redirect the result cache under tmp so tests never touch repo data."""
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path))


def test_small_output_passes_through():
    events: list[dict] = []
    budget = make_budget_tool_result(events)
    out = budget("noop_tool", "hello")
    assert out == "hello"
    assert events == []


def test_oversize_output_records_event():
    events: list[dict] = []
    budget = make_budget_tool_result(events)
    big = "x" * (TOOL_RESULT_BUDGET_BYTES + 1000)
    out = budget("big_tool", big)
    assert "truncated" in out.lower()
    # The note points at the REAL cache location and the retrieval tool.
    assert "data/result_cache/" in out
    assert "read_cached_result" in out
    assert ".whisper_cache" not in out
    assert len(events) == 1
    payload = events[0]["tool_result_truncated"]
    assert payload["tool_name"] == "big_tool"
    assert payload["full_size"] == len(big)
    assert payload["cache_path"] == f"data/result_cache/{payload['cache_filename']}"
    # Head+tail keep: nearly the whole budget survives, not a 2KB stub.
    assert payload["kept_bytes"] > TOOL_RESULT_BUDGET_BYTES // 2


def test_oversize_output_keeps_head_and_tail():
    budget = make_budget_tool_result(None)
    big = "HEAD_SENTINEL " + ("x" * (TOOL_RESULT_BUDGET_BYTES + 5000)) + " TAIL_SENTINEL"
    out = budget("big_tool", big)
    assert "HEAD_SENTINEL" in out
    assert "TAIL_SENTINEL" in out
    assert "characters omitted" in out


def test_legacy_budget_still_works_without_events():
    big = "y" * (TOOL_RESULT_BUDGET_BYTES + 100)
    # The legacy variant must still return a truncated string and not crash.
    assert "truncated" in _budget_tool_result("legacy_tool", big).lower()


# --- Prompt-payload exemption -------------------------------------------------
#
# A prompt skill's tool result is not data output — it is instructions wrapped
# around the user's input (a transcript), with the instructions at the TAIL.
# Passing that through the head-keeping budgeter silently discards the
# instructions, so the model never learns what to produce (the meeting_notes
# "notes were generated but nothing appeared in chat" bug). These pin that such
# payloads bypass the budgeter while genuine data output is still capped.


class _State:
    """Mimics the tool-state shape process_tool_results consumes."""

    def __init__(self, tool_id, tool_name, output):
        self.tool_id = tool_id
        self.tool_name = tool_name
        self.output = output
        self.side_effects = []
        self.status = "pending"


def _run(states, events):
    return asyncio.run(
        process_tool_results(
            states,
            budget_fn=make_budget_tool_result(events),
            session_approvals={},
            config={},
            model_id="test-model",
            recent_messages=[],
        )
    )


@pytest.fixture
def prompt_skill(monkeypatch):
    """Register a synthetic executorless (prompt) skill in SKILLS."""
    name = "fake_prompt_skill"
    monkeypatch.setitem(skills_mod.SKILLS, name, {"name": name, "body": "x", "executor": ""})
    return name


def test_produces_model_prompt_predicate(prompt_skill):
    # Prompt/folder skills (no executor) and prompt-emitting content executors.
    assert produces_model_prompt(prompt_skill) is True
    assert produces_model_prompt("summarize_transcript") is True
    assert produces_model_prompt("analyze_document") is True
    # Genuine data output and unknown/MCP names must still be budgeted.
    assert produces_model_prompt("ws_read_file") is False
    assert produces_model_prompt("definitely_unknown_tool") is False


def test_prompt_skill_output_not_budgeted(prompt_skill):
    # >50KB payload whose load-bearing instructions live at the tail.
    payload = ("transcript line. " * 4000) + "\n\nSKILL INSTRUCTIONS:\nSENTINEL_INSTRUCTIONS"
    assert len(payload.encode()) > TOOL_RESULT_BUDGET_BYTES
    events: list[dict] = []
    tool_results, _, _, _ = _run([_State("t1", prompt_skill, payload)], events)
    assert "SENTINEL_INSTRUCTIONS" in tool_results[0]["content"]  # instructions survive
    assert events == []  # no truncation event fired


def test_prompt_executor_output_not_budgeted():
    # analyze_document puts its ANALYSIS QUESTION at the tail — must survive.
    payload = ("document data " * 4000) + "\n\nANALYSIS QUESTION: SENTINEL_QUESTION"
    assert len(payload.encode()) > TOOL_RESULT_BUDGET_BYTES
    events: list[dict] = []
    tool_results, _, _, _ = _run([_State("t2", "analyze_document", payload)], events)
    assert "SENTINEL_QUESTION" in tool_results[0]["content"]
    assert events == []


def test_data_tool_output_still_budgeted():
    # Regression guard: non-prompt tool output is still capped — the MIDDLE is
    # dropped (head+tail survive) and a truncation event fires.
    payload = "HEAD" + ("x" * (TOOL_RESULT_BUDGET_BYTES + 5000)) + "TAIL_SENTINEL"
    events: list[dict] = []
    tool_results, _, _, _ = _run([_State("t3", "ws_read_file", payload)], events)
    content = tool_results[0]["content"]
    assert "truncated" in content.lower()
    assert "TAIL_SENTINEL" in content  # tail now survives by design
    assert "characters omitted" in content
    assert len(content) < len(payload)  # the middle was dropped
    assert len(events) == 1
