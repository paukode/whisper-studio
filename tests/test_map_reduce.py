"""Map-reduce condensation of oversized transcripts.

The map step (per-chunk extraction) is faked here so the tests are deterministic
and never touch the network or a local model. What matters:
  - below the threshold it is a pure no-op (no LLM calls),
  - above it the transcript is condensed but load-bearing content survives,
  - it NEVER returns an empty string (a failed map falls back to truncation),
  - the engine choice drives chunk sizing, and a runaway chunk count is capped,
  - execute_tool condenses a huge pasted-notes argument.
"""

import pytest

import server.skills as sk
import server.summarize.mapreduce as mr
from server.infrastructure import oneshot


@pytest.fixture(autouse=True)
def _clear_condense_cache():
    # The condensation cache is a module global; identical (text, config) across
    # tests would otherwise return a prior test's result. Clear between tests.
    mr._cache.clear()
    yield
    mr._cache.clear()


def _cfg(**over):
    base = {
        "enabled": True,
        "threshold_chars": 4000,
        "chunk_chars": 1500,
        "local_chunk_chars": 800,
        "overlap_chars": 100,
        "map_max_tokens": 500,
        "max_chunks": 40,
        "max_output_chars": 10_000_000,  # generous; individual tests override
        "local_max_output_chars": 10_000_000,
        "engine": "haiku",
    }
    base.update(over)
    return {"map_reduce_summary": base}


def _big(n):
    return "Alice: we discussed the migration and the roadmap.\n\n" * n


def test_under_threshold_is_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(mr, "one_shot", lambda *a, **k: calls.append(1) or "X")
    text = "a short transcript that fits fine"
    assert mr.maybe_condense_transcript(text, config=_cfg()) == text
    assert calls == []  # no map calls below the threshold


def test_condense_preserves_boundary_sentinel_and_marks_note(monkeypatch):
    # Echo each chunk body so a sentinel placed at a chunk boundary is observable.
    def echo(
        system, user, *, max_tokens, engine=None, cloud_model_key="haiku", local_model_key=None
    ):
        return user.split("\n\n", 1)[1]

    monkeypatch.setattr(mr, "one_shot", echo)
    monkeypatch.setattr(mr, "resolve_map_engine", lambda config=None: "haiku")
    body = _big(200)
    text = body + "\n\nACTION zara to fix the SAP extract SENTINEL_XYZ by friday.\n\n" + body
    assert len(text) > 4000
    out = mr.maybe_condense_transcript(text, config=_cfg())
    assert out.startswith("[NOTE: The transcript was too long")
    assert "SENTINEL_XYZ" in out  # overlap + concat keeps boundary material


def test_condense_compresses(monkeypatch):
    monkeypatch.setattr(mr, "one_shot", lambda s, u, **k: "DECISIONS: shipped it.")
    monkeypatch.setattr(mr, "resolve_map_engine", lambda config=None: "haiku")
    text = _big(400)
    out = mr.maybe_condense_transcript(text, config=_cfg())
    assert out.startswith("[NOTE: The transcript was too long")
    assert "DECISIONS: shipped it." in out
    assert len(out) < len(text)  # extracts are far smaller than the transcript


def test_all_map_failures_fall_back_to_truncation_not_empty(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("map down")

    monkeypatch.setattr(mr, "one_shot", boom)
    monkeypatch.setattr(mr, "resolve_map_engine", lambda config=None: "haiku")
    out = mr.maybe_condense_transcript(_big(300), config=_cfg())
    assert out  # never empty
    assert "truncated here" in out
    assert not out.startswith("[NOTE:")


def test_all_empty_extracts_fall_back(monkeypatch):
    monkeypatch.setattr(mr, "one_shot", lambda *a, **k: "")
    monkeypatch.setattr(mr, "resolve_map_engine", lambda config=None: "haiku")
    out = mr.maybe_condense_transcript(_big(300), config=_cfg())
    assert out and "truncated here" in out


def test_local_engine_produces_more_chunks(monkeypatch):
    seen = {"haiku": 0, "local": 0}

    def rec(
        system, user, *, max_tokens, engine=None, cloud_model_key="haiku", local_model_key=None
    ):
        seen[engine] += 1
        return "x"

    monkeypatch.setattr(mr, "one_shot", rec)
    text = _big(400)
    mr.maybe_condense_transcript(text, config=_cfg(engine="haiku"))
    mr.maybe_condense_transcript(text, config=_cfg(engine="local"))
    assert seen["local"] > seen["haiku"]  # the smaller local window yields more chunks


def test_max_chunks_cap(monkeypatch):
    calls = []
    monkeypatch.setattr(mr, "one_shot", lambda *a, **k: calls.append(1) or "x")
    monkeypatch.setattr(mr, "resolve_map_engine", lambda config=None: "haiku")
    # Tiny chunks would blow past the cap; re-chunking must keep it bounded.
    mr.maybe_condense_transcript(_big(2000), config=_cfg(chunk_chars=300, max_chunks=4))
    assert len(calls) <= 4


def test_repeat_calls_use_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(mr, "one_shot", lambda *a, **k: calls.append(1) or "x")
    monkeypatch.setattr(mr, "resolve_map_engine", lambda config=None: "haiku")
    mr._cache.clear()
    text = _big(400)
    first = mr.maybe_condense_transcript(text, config=_cfg())
    n_after_first = len(calls)
    second = mr.maybe_condense_transcript(text, config=_cfg())
    assert first == second
    assert len(calls) == n_after_first  # second call served from cache, no new map calls


def test_malformed_numeric_config_does_not_raise():
    # A non-numeric threshold coerces to the default instead of crashing the gate.
    out = mr.maybe_condense_transcript("small text", config=_cfg(threshold_chars="not-a-number"))
    assert out == "small text"
    assert mr.threshold(config=_cfg(threshold_chars="600k")) == mr._DEFAULTS["threshold_chars"]


def test_cache_rekeys_when_output_affecting_config_changes(monkeypatch):
    calls = []
    monkeypatch.setattr(mr, "one_shot", lambda *a, **k: calls.append(1) or "x")
    monkeypatch.setattr(mr, "resolve_map_engine", lambda config=None: "haiku")
    mr._cache.clear()
    text = _big(400)
    mr.maybe_condense_transcript(text, config=_cfg(max_chunks=40))
    after_first = len(calls)
    mr.maybe_condense_transcript(text, config=_cfg(max_chunks=3))  # different sig
    assert len(calls) > after_first  # not served from the stale entry


def test_output_cap_bounds_condensed_result(monkeypatch):
    monkeypatch.setattr(mr, "one_shot", lambda s, u, **k: "X" * 2000)
    text = _big(400)
    out = mr.maybe_condense_transcript(
        text,
        config=_cfg(engine="local", local_max_output_chars=1000, local_chunk_chars=1500),
    )
    body = out[len(mr.NOTE_PREFIX) :]
    assert "later segments omitted" in body
    assert len(body) < 2000  # bounded well under the raw extract volume


def test_map_all_preserves_order(monkeypatch):
    # The cloud path fans out concurrently; concatenation order must stay stable.
    monkeypatch.setattr(mr, "one_shot", lambda system, user, **k: user.split("\n\n", 1)[1])
    chunks = [f"chunk-{i}" for i in range(12)]
    assert mr._map_all(chunks, "haiku", {"map_max_tokens": 500}) == chunks


def test_char_split_handles_newline_sparse_input(monkeypatch):
    # A single giant line must not become one oversized chunk.
    seen = []
    monkeypatch.setattr(
        mr, "one_shot", lambda s, u, **k: seen.append(len(u.split("\n\n", 1)[1])) or "x"
    )
    monkeypatch.setattr(mr, "resolve_map_engine", lambda config=None: "haiku")
    one_line = "word " * 3000  # ~15000 chars, no paragraph breaks
    mr.maybe_condense_transcript(one_line, config=_cfg(threshold_chars=4000, chunk_chars=1500))
    assert seen and max(seen) <= 1500  # every chunk fed to the map fits chunk_chars


def test_is_claude_id():
    assert oneshot._is_claude_id("global.anthropic.claude-opus-4-8")
    assert oneshot._is_claude_id("global.anthropic.claude-haiku-4-5-20251001-v1:0")
    assert not oneshot._is_claude_id("openai.gpt-5.5")
    assert not oneshot._is_claude_id("local:gemma-4-12b-it-qat-q4_0")
    assert not oneshot._is_claude_id("global.anthropic.claude-fable-5")  # data-retention gated
    assert not oneshot._is_claude_id("")


def test_pick_claude_fallback_never_returns_non_claude(monkeypatch):
    models = {
        "gpt5.5": "openai.gpt-5.5",
        "opus4.8": "global.anthropic.claude-opus-4-8",
        "haiku": "global.anthropic.claude-haiku-4-5",
    }
    monkeypatch.setattr("server.chat.infra._get_default_model", lambda: "gpt5.5")
    assert oneshot._pick_claude_fallback(models).startswith("global.anthropic.claude")
    monkeypatch.setattr("server.chat.infra._get_default_model", lambda: "opus4.8")
    assert oneshot._pick_claude_fallback(models) == "global.anthropic.claude-opus-4-8"
    # No Claude in the catalogue -> None (caller raises rather than misroute).
    monkeypatch.setattr("server.chat.infra._get_default_model", lambda: "gpt5.5")
    assert oneshot._pick_claude_fallback({"gpt5.5": "openai.gpt-5.5", "l": "local:x"}) is None


def test_execute_tool_condenses_later_oversized_key(monkeypatch):
    sk.SKILLS = sk.load_skills()
    monkeypatch.setattr(mr, "one_shot", lambda s, u, **k: "DECISIONS: shipped.")
    monkeypatch.setattr(mr, "resolve_map_engine", lambda config=None: "haiku")
    monkeypatch.setattr(mr, "load_config", lambda *a, **k: {})
    monkeypatch.setitem(mr._DEFAULTS, "threshold_chars", 4000)
    monkeypatch.setitem(mr._DEFAULTS, "chunk_chars", 1500)
    big = _big(400)
    # 'notes' is short but present; the oversized 'text' must still be condensed.
    out = sk.execute_tool("meeting_notes", {"notes": "short", "text": big}, transcript="")
    assert "[NOTE: The transcript was too long" in out
    assert big not in out


def test_execute_tool_condenses_huge_literal_notes(monkeypatch):
    sk.SKILLS = sk.load_skills()
    monkeypatch.setattr(mr, "one_shot", lambda s, u, **k: "DECISIONS: shipped it.")
    monkeypatch.setattr(mr, "resolve_map_engine", lambda config=None: "haiku")
    monkeypatch.setattr(mr, "load_config", lambda *a, **k: {})  # force _DEFAULTS
    monkeypatch.setitem(mr._DEFAULTS, "threshold_chars", 4000)
    monkeypatch.setitem(mr._DEFAULTS, "chunk_chars", 1500)
    big = _big(400)
    out = sk.execute_tool("meeting_notes", {"notes": big}, transcript="")
    assert "[NOTE: The transcript was too long" in out  # condensed, not raw
    assert "DECISIONS: shipped it." in out
    assert len(out) < len(big)


def test_execute_tool_small_notes_untouched(monkeypatch):
    sk.SKILLS = sk.load_skills()
    calls = []
    monkeypatch.setattr(mr, "one_shot", lambda *a, **k: calls.append(1) or "x")
    monkeypatch.setattr(mr, "load_config", lambda *a, **k: {})
    out = sk.execute_tool("meeting_notes", {"notes": "quick sync, all good"}, transcript="")
    assert calls == []  # small input is never condensed
    assert "quick sync, all good" in out


# --- local map-model resolution (follow the active/resident model) -----------
#
# The local map step must NOT pin a fixed key: doing so evicts the resident chat
# model (only one ~7GB model is resident at a time) and fails outright when the
# fixed key is not the one that happens to be downloaded. These fake runtime.*
# so nothing touches a real model or the network.


def _fake_runtime(monkeypatch, *, resident, downloaded):
    """Patch server.local.runtime so is_downloaded/loaded_key/complete are fully
    faked and record which key complete() was actually handed. ``downloaded`` is
    the set of keys that report as present; a download attempt is a hard error."""
    from server.local import runtime as rt

    used = []
    monkeypatch.setattr(rt, "loaded_key", lambda: resident)
    monkeypatch.setattr(rt, "is_downloaded", lambda k: k in downloaded)

    def fake_complete(key, system, user, max_tokens):
        used.append(key)
        return "OUT"

    def no_download(*a, **k):
        raise AssertionError("must not download local weights")

    monkeypatch.setattr(rt, "complete", fake_complete)
    monkeypatch.setattr(rt, "ensure_downloaded", no_download)
    return used


def test_one_shot_local_follows_resident_model(monkeypatch):
    # A coder chat model is resident; the map must reuse it, not evict it for the
    # fixed default key.
    used = _fake_runtime(
        monkeypatch, resident="local_gemma_coder", downloaded={"local_gemma", "local_gemma_coder"}
    )
    out = oneshot.one_shot("sys", "usr", max_tokens=100, engine="local")
    assert out == "OUT"
    assert used == ["local_gemma_coder"]


def test_one_shot_local_prefers_passed_key_over_resident(monkeypatch):
    # An explicit local_model_key (the active chat model threaded from routes)
    # wins over whatever is currently resident.
    used = _fake_runtime(
        monkeypatch, resident="local_gemma", downloaded={"local_gemma", "local_gemma_coder"}
    )
    oneshot.one_shot(
        "sys", "usr", max_tokens=100, engine="local", local_model_key="local_gemma_coder"
    )
    assert used == ["local_gemma_coder"]


def test_one_shot_local_falls_back_to_downloaded_coder(monkeypatch):
    # Nothing resident and the default local_gemma is NOT downloaded; the first
    # downloaded model (the coder) runs the map instead of failing.
    used = _fake_runtime(monkeypatch, resident=None, downloaded={"local_gemma_coder"})
    out = oneshot.one_shot("sys", "usr", max_tokens=100, engine="local")
    assert out == "OUT"
    assert used == ["local_gemma_coder"]


def test_one_shot_local_never_downloads(monkeypatch):
    # Nothing resident and nothing downloaded: raise (so the caller truncates)
    # rather than let complete()/ensure_downloaded() pull multi-GB weights.
    used = _fake_runtime(monkeypatch, resident=None, downloaded=set())
    with pytest.raises(RuntimeError, match="not downloaded"):
        oneshot.one_shot("sys", "usr", max_tokens=100, engine="local")
    assert used == []  # complete() never ran


def test_condense_local_threads_chat_model_key(monkeypatch):
    # End-to-end through the real one_shot: a local condensation run steers every
    # map call at the chat model key threaded from the caller.
    used = _fake_runtime(
        monkeypatch, resident=None, downloaded={"local_gemma", "local_gemma_coder"}
    )

    def extract(key, system, user, max_tokens):
        used.append(key)
        return "DECISIONS: shipped it."

    from server.local import runtime as rt

    monkeypatch.setattr(rt, "complete", extract)
    out = mr.maybe_condense_transcript(
        _big(400), config=_cfg(engine="local"), chat_model_key="local_gemma_coder"
    )
    assert out.startswith("[NOTE: The transcript was too long")
    assert used and set(used) == {"local_gemma_coder"}
