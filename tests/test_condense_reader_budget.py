"""Transcript condensation sizing and caching contracts.

- The gate and the condensed output follow the READING chat model's input
  budget, not a fixed 600k chars and not the map engine.
- Chunk size and concurrency follow the map engine, including a specific
  on-device key (serial, local-sized chunks).
- Only a complete condensation is cached, under the RESOLVED map engine, so a
  failure, a partial failure or a mode switch never pins a degraded result.
- The hybrid one-shot writer Off runs no model at all, and Local mode never
  maps on a cloud engine.
- An on-device reader is sized for the context the turn asks for (the CTX
  chip), which the local route applies only after condensation runs, and a
  local map step starts the model at that size.
- A measured turn keeps the transcript raw while it fits beside the rest of
  that turn; only one that does not fit is condensed.
The map step and the model windows are faked: no network, no local model."""

from __future__ import annotations

import json
import threading
import time

import pytest

import server.summarize.mapreduce as mr
from server.chat.engine.windows import input_budget as _real_input_budget

SMALL, BIG = "reader_small", "reader_big"
_BUDGETS = {SMALL: 30_000, BIG: 872_000}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    mr._cache.clear()
    # Patch where _input_budget reads it (a lazy import of the windows module).
    monkeypatch.setattr(
        "server.chat.engine.windows.input_budget", lambda key, meta=None: _BUDGETS.get(key)
    )
    monkeypatch.setattr("server.local.runtime.is_local_model", lambda k: k == "local_gemma")
    yield
    mr._cache.clear()


def _cfg(**over) -> dict:
    base = {"engine": "auto", "local_chunk_chars": 2_000, "chunk_chars": 8_000}
    base.update(over)
    return {"map_reduce_summary": base}


def _text(chars: int) -> str:
    line = "Alice: we discussed the migration and the roadmap.\n\n"
    return (line * (chars // len(line) + 1))[:chars]


def _record(calls: list[dict], reply: str | None = "DECISIONS: shipped it."):
    def fake(system, user, *, max_tokens, engine=None, **kw):
        calls.append({"engine": engine, "chunk": user.split("\n\n", 1)[1], **kw})
        if reply is None:
            raise RuntimeError("map down")
        return reply

    return fake


def test_gate_follows_the_reader_budget(monkeypatch):
    cfg = mr._cfg(_cfg())
    small, big = mr._reader_limit(SMALL, cfg), mr._reader_limit(BIG, cfg)
    assert small < big <= cfg["threshold_chars"]
    text = _text(small + 5_000)
    assert len(text) < big
    calls: list[dict] = []
    monkeypatch.setattr(mr, "one_shot", _record(calls))
    monkeypatch.setattr(mr, "resolve_map_engine", lambda config=None: "haiku")
    # The same transcript fits the big reader untouched but is condensed for the
    # small one, where it would overflow.
    assert mr.maybe_condense_transcript(text, config=_cfg(), chat_model_key=BIG) == text
    assert calls == []
    out = mr.maybe_condense_transcript(text, config=_cfg(), chat_model_key=SMALL)
    assert out.startswith(mr.NOTE_PREFIX) and calls


def test_fallback_truncates_to_what_the_reader_takes(monkeypatch):
    cfg = mr._cfg(_cfg())
    limit = mr._reader_limit(SMALL, cfg)
    monkeypatch.setattr(mr, "one_shot", _record([], reply=None))
    monkeypatch.setattr(mr, "resolve_map_engine", lambda config=None: "haiku")
    text = _text(limit * 3)
    out = mr.maybe_condense_transcript(text, config=_cfg(), chat_model_key=SMALL)
    kept, marker = out.split("\n\n[Transcript truncated here", 1)
    assert text.startswith(kept) and len(kept) <= limit
    assert "map down" in marker  # the reason reaches the reader


@pytest.mark.parametrize(("map_engine", "reader"), [("local", BIG), ("haiku", SMALL)])
def test_output_cap_follows_the_reader_not_the_map_engine(monkeypatch, map_engine, reader):
    cfg = mr._cfg(_cfg())
    limit = mr._reader_limit(reader, cfg)
    monkeypatch.setattr(mr, "one_shot", _record([], reply="E" * 4_000))
    monkeypatch.setattr(mr, "resolve_map_engine", lambda config=None: map_engine)
    monkeypatch.setattr("server.local.runtime.loaded_key", lambda: "local_gemma")
    out = mr.maybe_condense_transcript(
        _text(cfg["threshold_chars"] + 1), config=_cfg(), chat_model_key=reader
    )
    body = out[len(mr.NOTE_PREFIX) :].split("\n\n[Some later segments omitted", 1)[0]
    assert len(body) <= limit
    if reader == BIG:
        # A 1M reader is not squeezed into the small-window cap of a local map.
        assert len(body) > cfg["local_max_output_chars"]


def test_specific_local_key_maps_serially_in_local_sized_chunks(monkeypatch):
    cfg = mr._cfg(_cfg())
    active = {"now": 0, "peak": 0}
    lock = threading.Lock()
    calls: list[dict] = []

    def fake(system, user, *, max_tokens, engine=None, **kw):
        with lock:
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
        time.sleep(0.002)
        with lock:
            active["now"] -= 1
        calls.append({"engine": engine, "chunk": user.split("\n\n", 1)[1]})
        return "x"

    monkeypatch.setattr(mr, "one_shot", fake)
    monkeypatch.setattr(mr, "resolve_map_engine", lambda config=None: "local_gemma")
    mr.maybe_condense_transcript(_text(40_000), config=_cfg(threshold_chars=10_000))
    assert len(calls) > 1
    assert {c["engine"] for c in calls} == {"local_gemma"}
    assert max(len(c["chunk"]) for c in calls) <= cfg["local_chunk_chars"]
    assert active["peak"] == 1  # one llama-server slot: never concurrent


def test_failed_condensation_is_retried_not_served_from_cache(monkeypatch):
    monkeypatch.setattr(mr, "resolve_map_engine", lambda config=None: "haiku")
    text = _text(20_000)
    cfg = _cfg(threshold_chars=10_000)
    monkeypatch.setattr(mr, "one_shot", _record([], reply=None))
    first = mr.maybe_condense_transcript(text, config=cfg)
    assert "truncated here" in first
    calls: list[dict] = []
    monkeypatch.setattr(mr, "one_shot", _record(calls))
    second = mr.maybe_condense_transcript(text, config=cfg)
    assert calls and second.startswith(mr.NOTE_PREFIX)


def test_partial_failure_is_not_cached(monkeypatch):
    monkeypatch.setattr(mr, "resolve_map_engine", lambda config=None: "haiku")
    text = _text(40_000)
    cfg = _cfg(threshold_chars=10_000)
    n = {"calls": 0}

    def flaky(system, user, *, max_tokens, **kw):
        n["calls"] += 1
        if n["calls"] == 1:
            raise RuntimeError("throttled")
        return "x"

    monkeypatch.setattr(mr, "one_shot", flaky)
    mr.maybe_condense_transcript(text, config=cfg)
    after_first = n["calls"]
    mr.maybe_condense_transcript(text, config=cfg)
    assert n["calls"] > after_first  # the hole is filled on the next turn


def test_cache_keys_on_the_resolved_engine(monkeypatch):
    text = _text(20_000)
    cfg = _cfg(threshold_chars=10_000)
    calls: list[dict] = []
    monkeypatch.setattr(mr, "one_shot", _record(calls))
    monkeypatch.setattr("server.local.runtime.loaded_key", lambda: "local_gemma")
    monkeypatch.setattr(mr, "resolve_map_engine", lambda config=None: "haiku")
    mr.maybe_condense_transcript(text, config=cfg)
    assert {c["engine"] for c in calls} == {"haiku"}
    calls.clear()
    # Same transcript, same config ("auto"), but the mode now resolves to the
    # on-device engine: the cloud result must not be served.
    monkeypatch.setattr(mr, "resolve_map_engine", lambda config=None: "local")
    mr.maybe_condense_transcript(text, config=cfg)
    assert calls and {c["engine"] for c in calls} == {"local"}


def test_writer_off_runs_no_model(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(mr, "one_shot", _record(calls))
    monkeypatch.setattr(mr, "resolve_map_engine", lambda config=None: "none")
    cfg = mr._cfg(_cfg())
    limit = mr._reader_limit(SMALL, cfg)
    out = mr.maybe_condense_transcript(_text(limit * 2), config=_cfg(), chat_model_key=SMALL)
    assert calls == []
    kept, marker = out.split("\n\n[Transcript truncated here", 1)
    assert len(kept) <= limit and "Off" in marker


def test_local_mode_never_maps_on_a_forced_cloud_engine(monkeypatch):
    from server.infrastructure import config as cfg_mod

    real = cfg_mod.load_config()
    monkeypatch.setattr(cfg_mod, "load_config", lambda *a, **k: {**real, "model_mode": "local"})
    monkeypatch.setattr("server.local.runtime.loaded_key", lambda: "local_gemma")
    calls: list[dict] = []
    monkeypatch.setattr(mr, "one_shot", _record(calls))
    mr.maybe_condense_transcript(_text(20_000), config=_cfg(engine="haiku", threshold_chars=10_000))
    assert calls and {c["engine"] for c in calls} == {"local"}


def test_local_reader_is_sized_for_the_requested_context(monkeypatch):
    """The chip's size wins over the resident or remembered window, which the
    turn is about to replace; a cloud reader ignores it."""
    live = {**_BUDGETS, "local_gemma": 60_000}

    def budget(key, meta=None):
        if meta and meta.get("context_window"):
            return _real_input_budget(key, meta)
        return live.get(key)

    monkeypatch.setattr("server.chat.engine.windows.input_budget", budget)
    cfg = mr._cfg(_cfg())
    resident = mr._reader_limit("local_gemma", cfg)
    requested = mr._reader_limit("local_gemma", cfg, 8_192)
    assert requested < resident
    assert mr._reader_limit(BIG, cfg, 8_192) == mr._reader_limit(BIG, cfg)

    calls: list[dict] = []
    monkeypatch.setattr(mr, "one_shot", _record(calls))
    monkeypatch.setattr(mr, "resolve_map_engine", lambda config=None: "haiku")
    text = _text(requested + 1_000)
    assert len(text) < resident
    # Fits the window the model is loaded at, not the one this turn starts it at.
    assert mr.maybe_condense_transcript(text, config=_cfg(), chat_model_key="local_gemma") == text
    out = mr.maybe_condense_transcript(
        text, config=_cfg(), chat_model_key="local_gemma", reader_n_ctx=8_192
    )
    assert out.startswith(mr.NOTE_PREFIX) and calls


def test_measured_turn_keeps_a_fitting_transcript_raw(monkeypatch):
    """The fixed-overhead limit would condense this transcript, but the turn
    that reads it leaves room for it: it goes to the reader untouched, with no
    map call. A turn that leaves no room condenses the same text."""
    cfg = mr._cfg(_cfg())
    limit = mr._reader_limit(SMALL, cfg)
    text = _text(limit + 5_000)
    calls: list[dict] = []
    monkeypatch.setattr(mr, "one_shot", _record(calls))
    monkeypatch.setattr(mr, "resolve_map_engine", lambda config=None: "haiku")
    light_turn = 2_000
    assert len(text) <= mr._raw_fit(SMALL, cfg, None, light_turn, limit)
    out = mr.maybe_condense_transcript(
        text, config=_cfg(), chat_model_key=SMALL, turn_tokens=light_turn
    )
    assert out == text and calls == []

    heavy_turn = _BUDGETS[SMALL] - len(text) // mr._CHARS_PER_TOKEN
    assert len(text) > mr._raw_fit(SMALL, cfg, None, heavy_turn, limit)
    out = mr.maybe_condense_transcript(
        text, config=_cfg(), chat_model_key=SMALL, turn_tokens=heavy_turn
    )
    assert out.startswith(mr.NOTE_PREFIX) and calls


def test_raw_fit_shrinks_as_the_turn_grows():
    cfg = mr._cfg(_cfg())
    limit = mr._reader_limit(SMALL, cfg)
    fits = [mr._raw_fit(SMALL, cfg, None, t, limit) for t in (0, 5_000, 20_000, 40_000)]
    assert fits == sorted(fits, reverse=True) and fits[-1] == 0
    # Never above the configured cap, and an unmeasured turn keeps the limit.
    assert mr._raw_fit(BIG, cfg, None, 0, mr._reader_limit(BIG, cfg)) <= cfg["threshold_chars"]
    assert mr._raw_fit(SMALL, cfg, None, None, limit) == limit


def test_turn_measure_follows_what_the_reader_is_sent(monkeypatch):
    """An on-device reader is measured on the lean local prompt, and on the tool
    schemas only when it supports tools; a cloud reader on both as given."""
    tools_on = {"local_gemma": False}
    monkeypatch.setattr("server.local.runtime.supports_tools", lambda k: tools_on.get(k, False))
    monkeypatch.setattr(
        "server.local.runtime.build_local_system_prompt", lambda *parts, **kw: "LEAN"
    )
    tools = [{"name": "t", "description": "d" * 8_000, "input_schema": {}}]
    history = [{"role": "user", "content": "h" * 4_000}]
    kw = {"system_prompt": "S" * 40_000, "tools": tools, "messages": history, "texts": ["q"]}
    cloud = mr.reader_turn_tokens(BIG, **kw)
    local_no_tools = mr.reader_turn_tokens("local_gemma", **kw)
    tools_on["local_gemma"] = True
    local_tools = mr.reader_turn_tokens("local_gemma", **kw)
    assert local_no_tools < local_tools < cloud
    # The history counts for every reader.
    assert mr.reader_turn_tokens("local_gemma", **{**kw, "messages": []}) < local_tools


def test_local_map_starts_the_model_at_the_requested_context(monkeypatch):
    from server.local import runtime as local_rt

    monkeypatch.setattr(local_rt, "_requested_n_ctx", None)
    monkeypatch.setattr("server.local.runtime.loaded_key", lambda: "local_gemma")
    monkeypatch.setattr(mr, "resolve_map_engine", lambda config=None: "local")
    seen: list[int | None] = []

    def fake(system, user, *, max_tokens, engine=None, **kw):
        seen.append(local_rt.requested_n_ctx())
        return "x"

    monkeypatch.setattr(mr, "one_shot", fake)
    mr.maybe_condense_transcript(
        _text(20_000),
        config=_cfg(threshold_chars=10_000),
        chat_model_key="local_gemma",
        reader_n_ctx=16_384,
    )
    assert seen and set(seen) == {16_384}


def test_chat_route_measures_the_turn_that_reads_the_transcript(monkeypatch):
    """The route hands the condenser the turn it assembled (a longer history is
    a bigger turn), and what the condenser returns is what the model reads."""
    from tests.golden_harness import (
        FakeBedrockClient,
        msg_end,
        msg_start,
        run_chat_turn,
        text_block,
    )

    seen: list[dict] = []

    def recorder(text, **kw):
        seen.append(kw)
        return "CONDENSER SAW IT"

    monkeypatch.setattr(mr, "maybe_condense_transcript", recorder)

    def turn(history):
        client = FakeBedrockClient([[msg_start(), *text_block("ok"), *msg_end()]])
        run_chat_turn(
            monkeypatch,
            client,
            {"question": "summarize", "transcript": "Alice: hi", "history": history},
        )
        return client.requests[0]

    first = turn([])
    longer = [
        {"role": "user", "content": "x" * 8_000},
        {"role": "assistant", "content": "y" * 8_000},
    ]
    turn(longer)
    assert [kw["chat_model_key"] for kw in seen] == ["opus5.0", "opus5.0"]
    assert 0 < seen[0]["turn_tokens"] < seen[1]["turn_tokens"]
    last_user = first["messages"][-1]["content"]
    text = last_user if isinstance(last_user, str) else json.dumps(last_user)
    assert "CONDENSER SAW IT" in text and "Alice: hi" not in text
