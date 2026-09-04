"""Compaction ladder: prune-and-remeasure before paying for a summary, and a
durable start/end bracket around every run with boot-time orphan detection."""

import asyncio
import json

import pytest

from server.chat import compaction, compaction_log


@pytest.fixture
def log_root(tmp_path, monkeypatch):
    monkeypatch.setattr(compaction_log, "data_root", lambda: str(tmp_path))
    return tmp_path


def _messages(n=12, result_chars=4000):
    """A history whose bulk is old tool results — exactly what pruning sheds."""
    msgs = []
    for i in range(n):
        msgs.append(
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": f"t{i}", "name": "x", "input": {}}],
            }
        )
        msgs.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": f"t{i}", "content": "y" * result_chars}
                ],
            }
        )
    return msgs


def _read_log(log_root, session="s1"):
    path = log_root / "compactions" / f"{session}.jsonl"
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def test_prune_alone_resolves_pressure_without_a_summary(log_root, monkeypatch):
    """When age-tiered pruning brings the history back under the trigger, no
    summary model call happens at all."""
    monkeypatch.setattr(compaction, "thresholds_for", lambda *_a, **_k: (60_000, 80_000))

    def no_summary(*a, **k):
        raise AssertionError("summary path must not run when pruning resolves pressure")

    monkeypatch.setattr(compaction, "_compact_messages_simple", no_summary)
    msgs = _messages(result_chars=10_000)
    assert compaction.estimate_message_size(msgs) > 60_000

    out = asyncio.run(
        compaction.compact_messages_with_claude(msgs, "model-id", session_id="s1", model_key="k")
    )
    assert compaction.estimate_message_size(out) <= 60_000
    records = _read_log(log_root)
    assert records[0]["event"] == "start"
    assert records[-1] == {**records[-1], "event": "end", "outcome": "pruned-only"}


def test_overflow_trigger_never_stops_at_the_prune_rung(log_root, monkeypatch):
    """A provider-reported overflow outranks the char estimate: even when the
    pruned size looks fine, the ladder continues past rung 0."""
    monkeypatch.setattr(compaction, "thresholds_for", lambda *_a, **_k: (10_000_000, 12_000_000))
    msgs = _messages()

    out = asyncio.run(
        compaction.compact_messages_with_claude(
            msgs, "model-id", session_id="s1", model_key="k", trigger="context-overflow"
        )
    )
    records = _read_log(log_root)
    assert records[-1]["outcome"] != "pruned-only"
    assert out  # some strategy still produced a history


def test_bracket_closes_with_error_outcome_when_a_strategy_raises(log_root, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("summarizer exploded")

    monkeypatch.setattr(compaction, "_compact_strategies", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(
            compaction.compact_messages_with_claude(
                _messages(), "model-id", session_id="s1", model_key="k"
            )
        )
    records = _read_log(log_root)
    assert records[0]["event"] == "start"
    assert records[-1]["event"] == "end"
    assert records[-1]["outcome"] == "error"


def test_orphaned_bracket_is_reconciled_at_boot(log_root):
    compaction_log.record_start("crashed-sess", trigger="pressure", before_chars=100)
    assert compaction_log.reconcile_orphans() == 1
    records = _read_log(log_root, "crashed-sess")
    assert [r["event"] for r in records] == ["start", "end"]
    assert records[-1]["outcome"] == "orphaned"
    # A clean log is not touched on the next boot.
    assert compaction_log.reconcile_orphans() == 0


def test_log_is_trimmed_to_the_record_cap(log_root, monkeypatch):
    monkeypatch.setattr(compaction_log, "MAX_RECORDS", 10)
    for i in range(20):
        compaction_log.record_end("s1", outcome="summary", after_chars=i)
    assert len(_read_log(log_root)) <= 10


def test_bad_session_id_never_writes(log_root):
    compaction_log.record_start("../evil", trigger="pressure", before_chars=1)
    assert not (log_root / "compactions").exists()
