"""The llama-server on-device backend: registry, process manager, SSE adapter.

These cover the parts that must hold for "drop a model in config and it works":
the registry merge + validation, the required-binary version guard,
and the streaming normalization (content / reasoning_content / tool_calls →
the app's existing SSE contract). No real model or subprocess is started.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from server.local import llama_server as LS
from server.local import registry as R
from server.local import server_stream as SS


def _drain(make_agen) -> list[str]:
    async def run():
        return [c async for c in make_agen()]

    return asyncio.run(run())


# ── registry ─────────────────────────────────────────────────────────────────


def test_registry_returns_builtins_when_config_adds_nothing(monkeypatch):
    monkeypatch.setattr(R, "_config_local_models", lambda: {})
    models = R.local_models()
    assert set(models) == set(R.BUILTIN_LOCAL_MODELS)
    assert models["local_gemma"]["id"] == "local:gemma-4-12b-it-qat-q4_0"


def test_registry_adds_a_config_only_model(monkeypatch):
    """The headline behavior: a brand-new family needs no code change."""
    monkeypatch.setattr(
        R,
        "_config_local_models",
        lambda: {
            "local_llama4": {
                "id": "local:llama-4-8b",
                "label": "Llama 4 8B (Local)",
                "repo_id": "org/Llama-4-8B-GGUF",
                "filename": "llama-4-8b-Q4_K_M.gguf",
                "dir": "llama-4-8b",
                "supports_tools": True,
                "supports_thinking": True,
            }
        },
    )
    models = R.local_models()
    assert "local_llama4" in models
    entry = models["local_llama4"]
    assert entry["id"] == "local:llama-4-8b"
    assert entry["ctx"] == R._DEFAULT_CTX  # defaulted, not required in config
    assert entry["supports_tools"] is True


def test_every_builtin_context_fits_the_full_tool_pool():
    """The full tool pool ("Tools: All") renders ~17.2K tokens, so any built-in
    left at 16K rejects the very first turn with exceed_context_size. Caught
    exactly that drift on local_gemma_coder, hence this guard."""
    _MIN_CTX_FOR_FULL_TOOLS = 32768
    for key, entry in R.BUILTIN_LOCAL_MODELS.items():
        assert entry["ctx"] >= _MIN_CTX_FOR_FULL_TOOLS, (
            f"{key} ctx={entry['ctx']} is too small for the full tool pool"
        )
    assert R._DEFAULT_CTX >= _MIN_CTX_FOR_FULL_TOOLS


def test_registry_drops_half_declared_model(monkeypatch):
    """Missing weight fields must be skipped loudly, not fail later at load."""
    monkeypatch.setattr(
        R,
        "_config_local_models",
        lambda: {"local_broken": {"id": "local:x", "label": "Broken", "supports_tools": True}},
    )
    assert "local_broken" not in R.local_models()


def test_config_overrides_builtin_field_without_restating_repo(monkeypatch):
    monkeypatch.setattr(R, "_config_local_models", lambda: {"local_gemma": {"ctx": 65536}})
    entry = R.local_models()["local_gemma"]
    assert entry["ctx"] == 65536
    # Untouched fields still come from the built-in.
    assert entry["filename"] == "gemma-4-12b-it-qat-q4_0.gguf"


def test_runtime_view_is_live(monkeypatch):
    """runtime.LOCAL_MODELS is a view, so a config change needs no restart."""
    import server.local.runtime as L

    monkeypatch.setattr(
        R,
        "_config_local_models",
        lambda: {
            "local_new": {
                "id": "local:new",
                "repo_id": "o/r",
                "filename": "n.gguf",
                "dir": "n",
            }
        },
    )
    assert L.is_local_model("local_new")
    assert L.key_for_id("local:new") == "local_new"
    assert "local_new" in list(L.LOCAL_MODELS)


def test_registry_reads_id_from_chat_models_not_meta(monkeypatch):
    """Regression: ids live in the parallel chat_models map. Reading them from
    chat_model_meta yields None and invents "local:<key>", which then overwrites
    a built-in's real sentinel and breaks key_for_id()."""
    cfg = {
        "chat_models": {"local_gemma": "local:gemma-4-12b-it-qat-q4_0"},
        "chat_model_meta": {"local_gemma": {"is_local": True, "label": "G"}},
    }
    monkeypatch.setattr("server.infrastructure.config.load_config", lambda *a, **k: cfg)
    assert R._config_local_models()["local_gemma"]["id"] == "local:gemma-4-12b-it-qat-q4_0"


# ── version guard: llama-server is REQUIRED, so failures must be loud ────────


def test_ensure_available_rejects_too_old_build(monkeypatch):
    monkeypatch.setattr(LS, "binary_path", lambda: "/usr/bin/llama-server")
    monkeypatch.setattr(LS, "installed_build", lambda: LS.MIN_BUILD - 1)
    with pytest.raises(LS.LlamaServerUnavailable) as e:
        LS.ensure_available()
    assert "brew upgrade" in str(e.value)  # actionable fix, not just a complaint


def test_ensure_available_explains_missing_binary(monkeypatch):
    monkeypatch.setattr(LS, "binary_path", lambda: None)
    with pytest.raises(LS.LlamaServerUnavailable) as e:
        LS.ensure_available()
    assert "setup.sh" in str(e.value)


def test_ensure_available_accepts_unknown_build(monkeypatch):
    """An unparseable version must not block startup."""
    monkeypatch.setattr(LS, "binary_path", lambda: "/usr/bin/llama-server")
    monkeypatch.setattr(LS, "installed_build", lambda: None)
    assert LS.ensure_available() == "/usr/bin/llama-server"


# ── streamed tool-call reassembly ────────────────────────────────────────────


def test_call_accumulator_reassembles_fragmented_arguments():
    acc = SS._CallAccumulator()
    acc.feed([{"index": 0, "id": "c1", "function": {"name": "ws_read_file"}}])
    acc.feed([{"index": 0, "function": {"arguments": '{"pa'}}])
    acc.feed([{"index": 0, "function": {"arguments": 'th":"a.py"}'}}])
    calls = acc.finish()
    assert calls == [{"id": "c1", "name": "ws_read_file", "input": {"path": "a.py"}}]


def test_call_accumulator_keeps_parallel_calls_separate():
    acc = SS._CallAccumulator()
    acc.feed(
        [{"index": 0, "id": "a", "function": {"name": "ws_read_file", "arguments": '{"path":"x"}'}}]
    )
    acc.feed(
        [{"index": 1, "id": "b", "function": {"name": "ws_read_file", "arguments": '{"path":"y"}'}}]
    )
    calls = acc.finish()
    assert [c["input"]["path"] for c in calls] == ["x", "y"]


def test_call_accumulator_survives_bad_json():
    acc = SS._CallAccumulator()
    acc.feed([{"index": 0, "id": "a", "function": {"name": "t", "arguments": "{not json"}}])
    assert acc.finish() == [{"id": "a", "name": "t", "input": {}}]


# ── SSE normalization ────────────────────────────────────────────────────────


def _fake_rounds(*rounds):
    """Stand in for _stream_round. Each round is a list of (kind, piece) plus an
    implicit terminating ("done", {...}) built from its trailing calls entry.

    A round may carry a third element: the OpenAI-shaped ``usage`` dict
    llama-server appends when asked for it. Omitting it stands in for a build
    that reports no usage at all.
    """
    box = {"i": 0}

    async def gen(base_url, payload):
        i = min(box["i"], len(rounds) - 1)
        box["i"] = i + 1
        pieces, calls, *rest = rounds[i]
        for kind, piece in pieces:
            yield (kind, piece)
        yield (
            "done",
            {
                "calls": calls,
                "finish_reason": "tool_calls" if calls else "stop",
                "usage": rest[0] if rest else {},
            },
        )

    return gen


def _usage_frames(blob: str) -> list[dict]:
    """Every usage payload in a drained SSE blob, in order."""
    out = []
    for line in blob.splitlines():
        if not line.startswith("data: ") or line[6:].strip() == "[DONE]":
            continue
        try:
            obj = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        if "usage" in obj:
            out.append(obj["usage"])
    return out


def _events(blob: str) -> dict:
    counts: dict[str, int] = {}
    for line in blob.splitlines():
        if not line.startswith("data: ") or line[6:].strip() == "[DONE]":
            continue
        try:
            obj = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        for k in obj:
            counts[k] = counts.get(k, 0) + 1
    return counts


def test_reasoning_becomes_thinking_events(monkeypatch):
    monkeypatch.setattr(
        SS, "_stream_round", _fake_rounds(([("thinking", "hmm"), ("text", "answer")], []))
    )
    blob = "".join(
        _drain(lambda: SS._run_loop("k", "http://x", [], [], {"thinking": True}, session_id="s"))
    )
    assert '"thinking_start": true' in blob
    assert '"thinking": "hmm"' in blob
    assert '"thinking_stop": true' in blob
    assert '"text": "answer"' in blob
    # thinking closes before the answer starts
    assert blob.index('"thinking_stop"') < blob.index('"text": "answer"')
    assert blob.strip().endswith("data: [DONE]")


def test_thinking_stop_emitted_when_round_has_no_answer_text(monkeypatch):
    monkeypatch.setattr(SS, "_stream_round", _fake_rounds(([("thinking", "only")], [])))
    blob = "".join(_drain(lambda: SS._run_loop("k", "http://x", [], [], {}, session_id="s")))
    assert '"thinking_start": true' in blob and '"thinking_stop": true' in blob


def test_tool_round_emits_skill_events_and_feeds_results_back(monkeypatch):
    calls = [{"id": "c1", "name": "ws_read_file", "input": {"path": "a.py"}}]
    monkeypatch.setattr(SS, "_stream_round", _fake_rounds(([], calls), ([("text", "done")], [])))

    async def fake_round(tool_uses, **kw):
        return (
            [{"type": "tool_result", "tool_use_id": "c1", "content": "print(1)"}],
            ['{"skill_result": "ws_read_file", "output": "print(1)"}'],
            False,
            False,
        )

    monkeypatch.setattr(SS, "run_tool_round", fake_round)
    convo: list[dict] = []
    blob = "".join(_drain(lambda: SS._run_loop("k", "http://x", convo, [], {}, session_id="s")))
    assert '"skill": "ws_read_file"' in blob
    assert '"path": "a.py"' in blob
    assert '"skill_result": "ws_read_file"' in blob
    assert '"text": "done"' in blob
    # Results go back as NATIVE tool messages, not folded into a user turn.
    tool_msgs = [m for m in convo if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["tool_call_id"] == "c1"
    assert not any(m.get("role") == "user" for m in convo)
    # And the assistant's calling turn is recorded in OpenAI shape.
    assistant = [m for m in convo if m.get("role") == "assistant"]
    assert assistant[0]["tool_calls"][0]["function"]["name"] == "ws_read_file"


def test_parallel_calls_execute_in_one_round(monkeypatch):
    calls = [
        {"id": "a", "name": "ws_read_file", "input": {"path": "x"}},
        {"id": "b", "name": "ws_read_file", "input": {"path": "y"}},
    ]
    monkeypatch.setattr(SS, "_stream_round", _fake_rounds(([], calls), ([("text", "ok")], [])))
    seen = {}

    async def fake_round(tool_uses, **kw):
        seen["n"] = len(tool_uses)
        return (
            [{"type": "tool_result", "tool_use_id": t["id"], "content": "c"} for t in tool_uses],
            [],
            False,
            False,
        )

    monkeypatch.setattr(SS, "run_tool_round", fake_round)
    _drain(lambda: SS._run_loop("k", "http://x", [], [], {}, session_id="s"))
    assert seen["n"] == 2  # the in-process path could not do this


def test_pause_on_approval_stashes_state_and_emits_no_answer(monkeypatch):
    calls = [{"id": "w1", "name": "ws_write_file", "input": {"path": "a.txt"}}]
    monkeypatch.setattr(SS, "_stream_round", _fake_rounds(([], calls), ([("text", "after")], [])))

    async def pause_round(tool_uses, **kw):
        return (
            [{"type": "tool_result", "tool_use_id": "w1", "content": "[Not executed]"}],
            ['{"approval_request": {"tool_use_id": "w1", "category": "write"}}'],
            True,
            False,
        )

    monkeypatch.setattr(SS, "run_tool_round", pause_round)
    SS._paused.pop("sess-x", None)
    blob = "".join(_drain(lambda: SS._run_loop("k", "http://x", [], [], {}, session_id="sess-x")))
    assert '"approval_request"' in blob
    assert '"text"' not in blob  # the destructive action did NOT run
    assert SS.has_pause("sess-x")
    assert blob.strip().endswith("data: [DONE]")
    SS._paused.pop("sess-x", None)


def test_resume_with_no_paused_state_ends_cleanly():
    SS._paused.pop("ghost", None)
    chunks = _drain(lambda: SS.resume_chat("ghost", {"tool_use_id": "x", "content": "y"}))
    assert chunks == ["data: [DONE]\n\n"]


def test_last_round_forbids_tools_so_the_model_must_answer(monkeypatch):
    """Parity with the cloud paths: the in-process loop died at the cap with a
    canned message instead of making the model synthesize an answer."""
    seen: list[str] = []

    async def gen(base_url, payload):
        seen.append(payload.get("tool_choice"))
        # Always ask for a call, so only the final-round guard can stop the loop.
        yield (
            "done",
            {"calls": [{"id": "c", "name": "t", "input": {}}], "finish_reason": "tool_calls"},
        )

    monkeypatch.setattr(SS, "_stream_round", gen)
    monkeypatch.setattr(SS, "_MAX_TOOL_ROUNDS", 3)

    async def fake_round(tool_uses, **kw):
        return ([{"type": "tool_result", "tool_use_id": "c", "content": "r"}], [], False, False)

    monkeypatch.setattr(SS, "run_tool_round", fake_round)
    _drain(lambda: SS._run_loop("k", "http://x", [], [{"type": "function"}], {}, session_id="s"))
    assert seen[:-1] == ["auto"] * (len(seen) - 1)
    assert seen[-1] == "none"


def test_coerces_loose_argument_types_against_the_schema():
    """Regression: the Coder emitted "path": ["dir"] for a string parameter and
    _exec_ws_list_dir died on .strip(). Bedrock validates against the schema
    first; llama-server hands through whatever the model produced."""
    schemas = [
        {
            "function": {
                "name": "ws_list_directory",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "limit": {"type": "integer"},
                        "deep": {"type": "boolean"},
                        "globs": {"type": "array"},
                    },
                },
            }
        }
    ]
    props = SS._schema_props(schemas)
    got = SS._coerce_call_args(
        "ws_list_directory",
        {"path": ["server"], "limit": "20", "deep": "true", "globs": "*.py"},
        props,
    )
    assert got == {"path": "server", "limit": 20, "deep": True, "globs": ["*.py"]}


def test_empty_and_null_string_args_become_empty_string():
    """Regression: the Coder emitted "path": [] for a string parameter, which
    reached _exec_ws_list_dir's .strip() and killed the call. It must become ""
    so the executor's own default applies."""
    schemas = [
        {
            "function": {
                "name": "ws_list_directory",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        }
    ]
    props = SS._schema_props(schemas)
    assert SS._coerce_call_args("ws_list_directory", {"path": []}, props) == {"path": ""}
    assert SS._coerce_call_args("ws_list_directory", {"path": None}, props) == {"path": ""}


def test_coercion_leaves_correct_and_ambiguous_values_alone():
    schemas = [
        {
            "function": {
                "name": "t",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        }
    ]
    props = SS._schema_props(schemas)
    # Already correct.
    assert SS._coerce_call_args("t", {"path": "a.py"}, props) == {"path": "a.py"}
    # Nested object into a string is ambiguous — pass through so the executor
    # can reject it with a clear validation error instead of us inventing a value.
    nested = {"path": {"a": 1}}
    assert SS._coerce_call_args("t", nested, props) == nested
    # Unknown tool / unknown param: untouched.
    assert SS._coerce_call_args("other", {"path": ["x"]}, props) == {"path": ["x"]}


def test_loop_coerces_before_dispatch(monkeypatch):
    calls = [{"id": "c1", "name": "ws_list_directory", "input": {"path": ["server"]}}]
    monkeypatch.setattr(SS, "_stream_round", _fake_rounds(([], calls), ([("text", "ok")], [])))
    seen = {}

    async def fake_round(tool_uses, **kw):
        seen["input"] = tool_uses[0]["input"]
        return ([{"type": "tool_result", "tool_use_id": "c1", "content": "r"}], [], False, False)

    monkeypatch.setattr(SS, "run_tool_round", fake_round)
    schemas = [
        {
            "function": {
                "name": "ws_list_directory",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        }
    ]
    _drain(lambda: SS._run_loop("k", "http://x", [], schemas, {}, session_id="s"))
    assert seen["input"] == {"path": "server"}  # a list would crash the executor


def test_empty_turn_gets_an_explanatory_line_not_a_blank_bubble(monkeypatch):
    """A round with no text and no calls must not render as an empty message —
    some fine-tunes decode tokens that yield neither after a tool result."""
    monkeypatch.setattr(SS, "_stream_round", _fake_rounds(([], [])))
    blob = "".join(_drain(lambda: SS._run_loop("k", "http://x", [], [], {}, session_id="s")))
    assert '"text"' in blob and "No answer" in blob
    assert '"usage"' in blob
    assert blob.strip().endswith("data: [DONE]")


def test_no_fallback_line_when_the_turn_produced_text(monkeypatch):
    monkeypatch.setattr(SS, "_stream_round", _fake_rounds(([("text", "real answer")], [])))
    blob = "".join(_drain(lambda: SS._run_loop("k", "http://x", [], [], {}, session_id="s")))
    assert "real answer" in blob
    assert "No answer" not in blob


def test_transport_error_surfaces_without_usage(monkeypatch):
    async def boom(base_url, payload):
        raise RuntimeError("connection refused")
        yield  # pragma: no cover - generator marker

    monkeypatch.setattr(SS, "_stream_round", boom)
    blob = "".join(_drain(lambda: SS._run_loop("k", "http://x", [], [], {}, session_id="s")))
    assert '"error"' in blob and "connection refused" in blob
    assert '"usage"' not in blob  # no cost line on failure
    assert blob.strip().endswith("data: [DONE]")


# ── token accounting ─────────────────────────────────────────────────────────
# On-device turns used to report a hardcoded 0 input tokens and a chars/4 guess
# for output, so the chat counter read "0 in" and the context meter stayed empty.
# llama-server reports real counts when the request asks for them.


def _fake_httpx(monkeypatch, lines: list[str], captured: dict) -> None:
    """Replace httpx.AsyncClient with one that replays ``lines`` as an SSE body."""
    import httpx

    class _Resp:
        status_code = 200

        async def aiter_lines(self):
            for line in lines:
                yield line

        async def aread(self):  # pragma: no cover - only used for non-200
            return b""

    class _Stream:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *exc):
            return False

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, json=None):
            captured["url"] = url
            captured["json"] = json
            return _Stream()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


def test_stream_round_asks_for_usage_and_reads_the_final_chunk(monkeypatch):
    """include_usage is what makes llama-server send token counts at all, and its
    usage chunk carries an EMPTY choices list — so it has to be read before the
    per-choice delta handling or it is dropped."""
    lines = [
        'data: {"choices":[{"delta":{"content":"hi"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":23,"completion_tokens":16,'
        '"total_tokens":39,"prompt_tokens_details":{"cached_tokens":18}}}',
        "data: [DONE]",
    ]
    captured: dict = {}
    _fake_httpx(monkeypatch, lines, captured)

    async def run():
        return [ev async for ev in SS._stream_round("http://x", {"model": "k"})]

    events = asyncio.run(run())
    assert captured["json"]["stream"] is True
    assert captured["json"]["stream_options"] == {"include_usage": True}
    assert ("text", "hi") in events
    kind, done = events[-1]
    assert kind == "done"
    assert done["finish_reason"] == "stop"
    assert done["usage"]["prompt_tokens"] == 23
    assert done["usage"]["completion_tokens"] == 16


def test_usage_frame_reports_measured_tokens_and_context(monkeypatch):
    monkeypatch.setattr(LS, "resident_n_ctx", lambda: 32768)
    usage = {"prompt_tokens": 1200, "completion_tokens": 716}
    monkeypatch.setattr(SS, "_stream_round", _fake_rounds(([("text", "answer")], [], usage)))
    blob = "".join(_drain(lambda: SS._run_loop("k", "http://x", [], [], {}, session_id="s")))
    (frame,) = _usage_frames(blob)
    assert frame["input_tokens"] == 1200
    assert frame["output_tokens"] == 716
    assert frame["total_input"] == 1200
    assert frame["total_output"] == 716
    assert frame["estimated_cost_usd"] == 0.0
    # Full prompt vs the process's --ctx-size drives the status bar's meter.
    assert frame["context_used"] == 1200
    assert frame["context_max"] == 32768


def test_cached_prefix_counts_as_cache_read_not_fresh_input(monkeypatch):
    """--cache-reuse means a follow-up round re-sends a prompt it does not
    re-prefill; counting the whole thing as input would inflate every round."""
    monkeypatch.setattr(LS, "resident_n_ctx", lambda: 16384)
    usage = {
        "prompt_tokens": 5000,
        "completion_tokens": 40,
        "prompt_tokens_details": {"cached_tokens": 4800},
    }
    monkeypatch.setattr(SS, "_stream_round", _fake_rounds(([("text", "ok")], [], usage)))
    blob = "".join(_drain(lambda: SS._run_loop("k", "http://x", [], [], {}, session_id="s")))
    (frame,) = _usage_frames(blob)
    assert frame["input_tokens"] == 200
    assert frame["cache_read_tokens"] == 4800
    # The meter still shows the whole conversation, cached or not.
    assert frame["context_used"] == 5000


def test_usage_accumulates_across_tool_rounds(monkeypatch):
    """One frame per round, with running totals — the counter climbs during a
    tool turn instead of staying blank until the last round."""
    monkeypatch.setattr(LS, "resident_n_ctx", lambda: 32768)
    calls = [{"id": "c1", "name": "ws_read_file", "input": {"path": "a.py"}}]
    monkeypatch.setattr(
        SS,
        "_stream_round",
        _fake_rounds(
            ([], calls, {"prompt_tokens": 900, "completion_tokens": 30}),
            ([("text", "done")], [], {"prompt_tokens": 1500, "completion_tokens": 50}),
        ),
    )

    async def fake_round(tool_uses, **kw):
        return ([{"tool_use_id": "c1", "content": "file body"}], [], False, False)

    monkeypatch.setattr(SS, "run_tool_round", fake_round)
    blob = "".join(_drain(lambda: SS._run_loop("k", "http://x", [], [], {}, session_id="s")))
    first, second = _usage_frames(blob)
    assert (first["input_tokens"], first["total_input"]) == (900, 900)
    assert (second["input_tokens"], second["total_input"]) == (1500, 2400)
    assert second["total_output"] == 80
    # The meter tracks the LATEST prompt size, not the sum.
    assert second["context_used"] == 1500


def test_resume_continues_the_token_tally(monkeypatch):
    """An approval pause splits a turn across two requests; the counter must not
    restart at zero on the second half."""
    monkeypatch.setattr(LS, "resident_n_ctx", lambda: 32768)
    calls = [{"id": "c1", "name": "shell_command", "input": {"command": "rm -rf x"}}]
    monkeypatch.setattr(
        SS,
        "_stream_round",
        _fake_rounds(
            ([], calls, {"prompt_tokens": 700, "completion_tokens": 25}),
            ([("text", "after")], [], {"prompt_tokens": 1100, "completion_tokens": 15}),
        ),
    )

    async def pauses(tool_uses, **kw):
        return ([{"tool_use_id": "c1", "content": "pending"}], [], True, False)

    monkeypatch.setattr(SS, "run_tool_round", pauses)
    _drain(lambda: SS._run_loop("k", "http://x", [], [], {}, session_id="s"))
    assert SS.has_pause("s")
    blob = "".join(
        _drain(lambda: SS.resume_chat("s", {"tool_use_id": "c1", "content": "approved"}))
    )
    (frame,) = _usage_frames(blob)
    assert frame["total_input"] == 1800  # 700 before the pause + 1100 after
    assert frame["total_output"] == 40


def test_usage_falls_back_to_a_char_estimate_when_the_build_sends_none(monkeypatch):
    """An older llama-server that ignores include_usage must still report
    something, and must not claim a context reading it does not have."""
    monkeypatch.setattr(LS, "resident_n_ctx", lambda: 32768)
    monkeypatch.setattr(SS, "_stream_round", _fake_rounds(([("text", "x" * 400)], [])))
    blob = "".join(_drain(lambda: SS._run_loop("k", "http://x", [], [], {}, session_id="s")))
    (frame,) = _usage_frames(blob)
    assert frame["input_tokens"] == 0
    assert frame["output_tokens"] == 100  # 400 chars / 4
    assert "context_used" not in frame and "context_max" not in frame


def test_context_is_recorded_for_the_stats_panel(monkeypatch):
    """The Stats panel reads context from /api/costs, which reads the same
    per-session record the cloud loop keeps."""
    from server.chat.loop_hints import context_estimate, reset_session

    monkeypatch.setattr(LS, "resident_n_ctx", lambda: 65536)
    reset_session("stats-sess")
    usage = {"prompt_tokens": 4321, "completion_tokens": 10}
    monkeypatch.setattr(SS, "_stream_round", _fake_rounds(([("text", "ok")], [], usage)))
    _drain(lambda: SS._run_loop("k", "http://x", [], [], {}, session_id="stats-sess"))
    assert context_estimate("stats-sess") == (4321, 65536)
    reset_session("stats-sess")


def test_no_context_fields_when_no_model_server_is_resident(monkeypatch):
    """resident_n_ctx() is None between runs; the frame must still be valid."""
    monkeypatch.setattr(LS, "resident_n_ctx", lambda: None)
    usage = {"prompt_tokens": 100, "completion_tokens": 5}
    monkeypatch.setattr(SS, "_stream_round", _fake_rounds(([("text", "ok")], [], usage)))
    blob = "".join(_drain(lambda: SS._run_loop("k", "http://x", [], [], {}, session_id="s")))
    (frame,) = _usage_frames(blob)
    assert frame["input_tokens"] == 100
    assert "context_max" not in frame


def test_start_failure_is_reported_to_the_client(monkeypatch):
    def boom(key, n_ctx=None):
        raise LS.LlamaServerUnavailable("llama-server is not installed")

    monkeypatch.setattr(LS, "ensure_serving", boom)
    blob = "".join(
        _drain(lambda: SS.stream_chat("local_gemma", "sys", [], "s", tools=False, tool_ctx={}))
    )
    assert '"error"' in blob and "not installed" in blob
    assert blob.strip().endswith("data: [DONE]")


def test_complete_strips_leaked_reasoning_markers():
    """Observed live: on the non-streaming endpoint a model's thought channel can
    go unclaimed by upstream's parser, so reasoning_content is empty and content
    arrives as "<channel|>the answer". complete()'s callers (session summaries,
    index extraction) want clean text, not markers."""
    f = LS._strip_reasoning_markers
    assert f("<channel|>The repo is a web app.") == "The repo is a web app."
    # Whole thought block ahead of the close marker is dropped.
    assert f("<|channel>thought\nlet me think\n<channel|>Final answer.") == "Final answer."
    # Last close marker wins, so a marker quoted mid-reasoning can't truncate early.
    assert f("<channel|>a<channel|>b") == "b"
    # Qwen/DeepSeek-style markers too.
    assert f("<think>hmm</think>Answer.") == "Answer."
    # Marker-free text is untouched (the normal case for a well-behaved model).
    assert f("Just a plain answer.") == "Just a plain answer."
    assert f("") == ""


def test_reap_orphans_kills_only_our_model_servers(monkeypatch):
    """SIGKILL skips the shutdown hook and the child runs in its own session, so
    a force-quit leaves a llama-server holding gigabytes. Startup reaps those —
    but must never touch a llama-server the user runs for their own purposes."""
    import server.local.runtime as L

    models_dir = L.MODELS_DIR
    ps_out = (
        f"  111 /opt/homebrew/bin/llama-server --model {models_dir}/g/g.gguf --jinja\n"
        "  222 /opt/homebrew/bin/llama-server --model /home/me/other/model.gguf --jinja\n"
        "  333 /usr/bin/some-other-process --model foo\n"
    )

    class R:
        stdout = ps_out

    monkeypatch.setattr(LS.subprocess, "run", lambda *a, **k: R())
    killed = []
    monkeypatch.setattr(LS.os, "kill", lambda pid, sig: killed.append(pid))
    assert LS.reap_orphans() == 1
    assert killed == [111]  # not 222 (user's own), not 333 (not llama-server)


def test_reap_orphans_survives_a_dead_pid(monkeypatch):
    import server.local.runtime as L

    class R:
        stdout = f"  111 llama-server --model {L.MODELS_DIR}/g/g.gguf\n"

    monkeypatch.setattr(LS.subprocess, "run", lambda *a, **k: R())

    def gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(LS.os, "kill", gone)
    assert LS.reap_orphans() == 0  # counted only when the signal lands


def test_ensure_serving_honors_sticky_requested_ctx(monkeypatch):
    """The chat-input context slider must mean the same on both backends: a lazy
    start has to keep the user's last explicit size, not revert to the default."""
    import server.local.runtime as L

    monkeypatch.setattr(LS, "ensure_available", lambda: "/usr/bin/llama-server")
    monkeypatch.setattr(L, "ensure_downloaded", lambda key: "/models/x.gguf")
    monkeypatch.setattr(LS, "stop", lambda: None)
    monkeypatch.setattr(LS, "_wait_healthy", lambda port, proc, deadline: True)
    monkeypatch.setattr(L, "requested_n_ctx", lambda: 65536)
    seen = {}

    class FakeProc:
        pid = 4242

        def poll(self):
            return None

    def fake_popen(cmd, **kw):
        seen["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(LS.subprocess, "Popen", fake_popen)
    LS.ensure_serving("local_gemma")  # no explicit n_ctx
    cmd = seen["cmd"]
    assert cmd[cmd.index("--ctx-size") + 1] == "65536"
    # And the memory-critical flags are present.
    assert "--jinja" in cmd
    assert cmd[cmd.index("--parallel") + 1] == "1"
    # The context meter's denominator must be the size actually launched with.
    assert LS.resident_n_ctx() == 65536
    LS._state.update(key=None, port=None, n_ctx=None)
    LS._proc = None


def test_resident_n_ctx_is_none_once_the_process_is_gone():
    class DeadProc:
        def poll(self):
            return 1

    LS._proc = DeadProc()
    LS._state.update(key="local_gemma", port=1234, n_ctx=32768)
    try:
        assert LS.resident_n_ctx() is None  # stale size must not reach the meter
    finally:
        LS._state.update(key=None, port=None, n_ctx=None)
        LS._proc = None
    assert LS.resident_n_ctx() is None


def test_requested_n_ctx_setter_ignores_none():
    import server.local.runtime as L

    L.set_requested_n_ctx(32768)
    assert L.requested_n_ctx() == 32768
    L.set_requested_n_ctx(None)  # a lazy load must not clear the choice
    assert L.requested_n_ctx() == 32768
    L._requested_n_ctx = None


def test_messages_flatten_bedrock_blocks_to_openai():
    msgs = SS._to_openai_messages(
        "SYS",
        [
            {"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "image"}]},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": [{"type": "image"}]},  # nothing renderable
        ],
    )
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[1] == {"role": "user", "content": "hi"}
    assert msgs[2] == {"role": "assistant", "content": "hello"}
    assert len(msgs) == 3  # the image-only turn is dropped, not sent empty
