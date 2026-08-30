"""The llama-server on-device backend: registry, process manager, SSE adapter.

These cover the parts that must hold for "drop a model in config and it works":
the registry merge + validation, the required-binary version guard,
and the streaming normalization (content / reasoning_content / tool_calls →
the app's existing SSE contract). No real model or subprocess is started.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from server.local import llama_server as LS
from server.local import registry as R
from server.local import server_stream as SS


def _drain(make_agen) -> list[str]:
    async def run():
        return [c async for c in make_agen()]

    return asyncio.run(run())


@pytest.fixture(autouse=True)
def _seed_local_registry(monkeypatch):
    """A fresh checkout ships no local chat models (config-driven registry,
    empty by default). Seed the two recommended entries these tests reference
    by name, so they don't silently depend on the developer's real,
    gitignored config.json. A test that needs a different registry shape
    (empty, config-only, etc.) overrides _config_local_models itself, which
    wins since it runs after this fixture."""
    from server.local import registry as _reg

    monkeypatch.setattr(
        _reg,
        "_config_local_models",
        lambda: {
            key: dict(_reg.RECOMMENDED_LOCAL_MODELS[key])
            for key in ("local_gemma", "local_gemma_coder")
        },
    )


# ── registry ─────────────────────────────────────────────────────────────────


def test_registry_is_empty_when_nothing_installed_or_downloaded(monkeypatch):
    """The app ships no local models: a fresh catalog with no config entries and
    nothing on disk is empty until the user downloads a recommendation."""
    monkeypatch.setattr(R, "_config_local_models", lambda: {})
    monkeypatch.setattr(R, "_recommended_downloaded_on_disk", lambda entry: False)
    assert R.local_models() == {}


def test_registry_surfaces_a_downloaded_recommendation(monkeypatch):
    """A recommended model appears once its weights are on disk, even with no
    config entry (e.g. downloaded by a prior release) — so it is never orphaned."""
    monkeypatch.setattr(R, "_config_local_models", lambda: {})
    monkeypatch.setattr(
        R,
        "_recommended_downloaded_on_disk",
        lambda entry: entry.get("id") == "local:gemma-4-12b-it-qat-q4_0",
    )
    models = R.local_models()
    assert set(models) == {"local_gemma"}
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


def test_every_recommended_context_fits_the_full_tool_pool():
    """The full tool pool ("Tools: All") renders ~17.2K tokens, so any model left
    at 16K rejects the very first turn with exceed_context_size. Caught exactly
    that drift on local_gemma_coder, hence this guard over the curated set."""
    _MIN_CTX_FOR_FULL_TOOLS = 32768
    for key, entry in R.RECOMMENDED_LOCAL_MODELS.items():
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


def test_config_overrides_downloaded_recommendation_field(monkeypatch):
    """A config entry can retune one field of a downloaded recommendation without
    restating its repo — the recommended definition fills the untouched fields."""
    monkeypatch.setattr(
        R,
        "_recommended_downloaded_on_disk",
        lambda entry: entry.get("id") == "local:gemma-4-12b-it-qat-q4_0",
    )
    monkeypatch.setattr(R, "_config_local_models", lambda: {"local_gemma": {"ctx": 65536}})
    entry = R.local_models()["local_gemma"]
    assert entry["ctx"] == 65536
    # Untouched fields still come from the recommended definition.
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

        def stream(self, method, url, json=None):  # noqa: F811 — httpx kwarg name
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


def test_start_failure_is_reported_to_the_client(monkeypatch):
    """A llama-server that cannot start must surface a clean SSE error."""
    import asyncio

    from server.local import route as R
    from server.local import serving

    # Stub the facade seam the route consumes: the engine-level function sits
    # behind availability/registry/download preambles that need a real
    # llama-server binary and installed weights (absent on CI runners).
    def boom(model_key, n_ctx=None, **kw):
        raise RuntimeError("binary too old: need b10090")

    monkeypatch.setattr(serving, "ensure_serving", boom)
    resp = R.local_chat_response(
        model_key="local_gemma",
        body={},
        messages=[{"role": "user", "content": "hi"}],
        session_id="s-fail",
        approved_tool_result=None,
        transcript="",
        whisper_md_context="",
        memory_context="",
        session_memory_context="",
        plan_mode=False,
        mode="default",
        ws_path=None,
        session_approvals={},
        session_denials={},
        session_config={},
    )

    async def drain():
        return [c async for c in resp.body_iterator]

    blob = "".join(asyncio.run(drain()))
    assert "binary too old" in blob
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


def test_reap_orphans_matches_a_symlinked_models_dir(monkeypatch, tmp_path):
    """A checkout behind a symlink (worktree under /tmp, symlinked home or dev
    directory) gives MODELS_DIR two spellings. We spawn the child with the
    unresolved one, so a realpath-only marker misses our own orphan and leaks a
    server holding gigabytes — exactly what reaping exists to prevent."""
    import server.local.runtime as L

    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    unresolved = str(tmp_path / "link" / "models")
    resolved = os.path.realpath(unresolved)
    assert unresolved != resolved  # the premise of the bug

    monkeypatch.setattr(L, "MODELS_DIR", unresolved)

    class R:
        # 111 as we spawn it, 222 as ps would show it after resolution.
        stdout = (
            f"  111 /opt/homebrew/bin/llama-server --model {unresolved}/g/g.gguf --jinja\n"
            f"  222 /opt/homebrew/bin/llama-server --model {resolved}/g/g.gguf --jinja\n"
            "  333 /opt/homebrew/bin/llama-server --model /home/me/other/model.gguf\n"
        )

    monkeypatch.setattr(LS.subprocess, "run", lambda *a, **k: R())
    killed = []
    monkeypatch.setattr(LS.os, "kill", lambda pid, sig: killed.append(pid))
    assert LS.reap_orphans() == 2
    assert killed == [111, 222]  # still not 333 (the user's own)


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


def test_ensure_serving_coalesces_concurrent_duplicate_loads(monkeypatch):
    """Regression: the UI fired four /api/local-model/load for local_gemma at
    once. Each call ran stop()+spawn, so every llama-server was SIGKILLed ~1s in
    by the next duplicate and none became ready ("failed to become ready").
    Concurrent duplicate loads must coalesce onto a single spawn."""
    import threading
    import time as _time

    import server.local.runtime as L

    monkeypatch.setattr(LS, "ensure_available", lambda: "/usr/bin/llama-server")
    monkeypatch.setattr(L, "ensure_downloaded", lambda key: "/models/x.gguf")
    monkeypatch.setattr(L, "requested_n_ctx", lambda: 32768)
    monkeypatch.setattr(LS, "_wait_healthy", lambda port, proc, deadline: True)

    stops: list[int] = []
    monkeypatch.setattr(LS, "stop", lambda: stops.append(1))

    spawns: list[list] = []

    class FakeProc:
        pid = 4242

        def poll(self):
            return None

    def fake_popen(cmd, **kw):
        spawns.append(cmd)
        _time.sleep(0.05)  # cold start: hold the load lock so the others queue
        return FakeProc()

    monkeypatch.setattr(LS.subprocess, "Popen", fake_popen)

    LS._proc = None
    LS._state.update(key=None, port=None, n_ctx=None)

    barrier = threading.Barrier(4)
    errors: list[Exception] = []

    def worker():
        try:
            barrier.wait()  # release all four into ensure_serving together
            LS.ensure_serving("local_gemma", n_ctx=32768)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert errors == []
        assert len(spawns) == 1  # three duplicates reused the first spawn
        assert len(stops) == 1  # not one stop-and-restart per request
    finally:
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
