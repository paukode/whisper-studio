"""The tool-call check a finished local-chat download gets.

The model server is an httpx MockTransport that streams scripted
/v1/chat/completions SSE, so each run goes through the real LocalAdapter
round and the real stream parser, as a chat turn does. The serving layer is
stubbed with the same signatures as the real one.
"""

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.models_manager import manager, sizes, tool_check
from server.models_manager.catalog import get_entry
from server.models_manager.routes import router

KEY = "local_gemma"


def _sse(frames) -> httpx.Response:
    body = "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def call_reply(name, args) -> list[dict]:
    """A streamed round that ends in one tool call (arguments as a JSON string,
    split over two deltas like llama-server sends them)."""
    raw = json.dumps(args)
    head, tail = raw[: len(raw) // 2], raw[len(raw) // 2 :]
    return [
        {"choices": [{"delta": {"content": "I'll look at the workspace first."}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": name, "arguments": head},
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": tail}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 900, "completion_tokens": 40}},
    ]


def text_reply(text, finish="stop") -> list[dict]:
    return [
        {"choices": [{"delta": {"content": text}}]},
        {"choices": [{"delta": {}, "finish_reason": finish}]},
        {"choices": [], "usage": {"prompt_tokens": 900, "completion_tokens": 30}},
    ]


LIST_ROOT = call_reply("ws_list_directory", {"path": "."})


@pytest.fixture()
def stack(tmp_path, monkeypatch):
    """One tool-capable local model, a scripted model server, a stub serving
    layer that records how the check holds and releases the model."""
    monkeypatch.setenv("WHISPER_HOME", str(tmp_path))
    # models/ is user data: it lives under user_root(), not WHISPER_HOME.
    monkeypatch.setenv("WHISPER_USER_DIR", str(tmp_path))
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "models").mkdir()
    from server.local import registry, serving

    monkeypatch.setattr(
        registry,
        "_config_local_models",
        lambda: {KEY: dict(registry.RECOMMENDED_LOCAL_MODELS[KEY])},
    )
    state = SimpleNamespace(
        replies=[LIST_ROOT],
        requests=[],
        busy=0,
        ended=0,
        stopped=[],
        resident=None,
        load_error=None,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        state.requests.append(body)
        frames = state.replies[min(len(state.requests), len(state.replies)) - 1]
        return _sse(frames)

    real_async_client = httpx.AsyncClient

    def _client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory)

    # Same signature as serving.ensure_serving, so a drift fails here.
    def _ensure_serving(key, n_ctx=None, *, should_abort=None, mark_busy=False, started=None):
        if state.load_error:
            raise RuntimeError(state.load_error)
        if mark_busy:
            state.busy += 1
        state.resident = key
        return "http://llama.test"

    def _end_turn():
        state.ended += 1

    def _stop_if_idle(key, started=None):
        state.stopped.append(key)
        return True

    monkeypatch.setattr(serving, "ensure_serving", _ensure_serving)
    monkeypatch.setattr(serving, "end_turn", _end_turn)
    monkeypatch.setattr(serving, "stop_if_idle", _stop_if_idle)
    monkeypatch.setattr(serving, "resident_key", lambda: state.resident)
    monkeypatch.setattr(serving, "wire_model", lambda key: "gemma")
    monkeypatch.setattr(serving, "supports_tool_choice", lambda key: True)
    # The catalog route prefetches real sizes from Hugging Face in a thread;
    # left live, it reaches the network and fills the shared size cache that
    # later tests read their fallback sizes from.
    monkeypatch.setattr(sizes, "fetch_size", lambda repo_id, filename=None: None)
    monkeypatch.setattr(sizes, "prefetch", lambda entries: None)
    manager._jobs.clear()
    manager._queue.clear()
    manager._errors.clear()
    return state


# ── what the check sends ─────────────────────────────────────────────────


def test_check_sends_a_first_chat_turn(stack):
    """Each run is the request a fresh chat turn with a workspace sends: the
    adapter's own wire request over the first-round tool pool."""
    from server.chat.engine.local import LocalAdapter
    from server.local import runtime

    tool_check.run_check(KEY)

    tools, _core = tool_check.fresh_session_tools()
    adapter = LocalAdapter(
        model_key=KEY,
        base_url="http://llama.test",
        system_prompt=runtime.build_local_system_prompt(
            tools=True, ws_path=tool_check._EXAMPLE_WORKSPACE
        ),
        thinking=runtime.supports_thinking(KEY),
        tools_enabled=True,
        wire_model="gemma",
    )
    expected = adapter.wire_request(
        [{"role": "user", "content": tool_check.PROMPT}], tools, is_last_round=False
    )
    assert len(stack.requests) == tool_check.RUNS
    for sent in stack.requests:
        assert {k: v for k, v in sent.items() if k not in ("stream", "stream_options")} == expected
    assert "workspace is connected" in expected["messages"][0]["content"]


def test_fresh_session_tools_match_the_first_chat_round():
    """The pool the check offers is the one the turn engine advertises on round
    one of a new session with a workspace connected."""
    from server.chat.engine.runner import _assemble_round_tools

    ctx = SimpleNamespace(
        plan_mode=False,
        ws_path="/tmp/example",
        suppress_ws_search=False,
        effort_label=None,
        session_id="a-session-with-no-activations",
    )
    engine_tools, engine_core = _assemble_round_tools(ctx)
    tools, core = tool_check.fresh_session_tools()
    assert [t["name"] for t in tools] == [t["name"] for t in engine_tools]
    assert core == engine_core


# ── pass and fail ────────────────────────────────────────────────────────


def test_every_run_calling_a_tool_passes(stack):
    result = tool_check.run_check(KEY)

    assert result["state"] == "passed"
    assert result["passed"] == result["runs"] == result["total"] == tool_check.RUNS
    assert tool_check.result_of(KEY) == result


@pytest.mark.parametrize(
    "reply, reason",
    [
        (text_reply('```json\n{"name": "ws_list_directory"}\n```'), "without calling a tool"),
        (text_reply("I will now list the files and", finish="length"), "output limit"),
        (call_reply("open_the_repo", {"path": "."}), "not offered (open_the_repo)"),
        (call_reply("ws_read_file", {}), "ws_read_file without path"),
        (call_reply("ws_read_file", {"path": [{"a": 1}, {"b": 2}]}), "path of the wrong type"),
    ],
)
def test_a_run_without_a_usable_call_fails_the_check(stack, reply, reason):
    stack.replies = [reply]

    result = tool_check.run_check(KEY)

    assert result["state"] == "failed"
    assert reason in result["reason"]
    assert result["passed"] == 0
    # The first failed run decides it: no more model time is spent.
    assert result["runs"] == 1


def test_coerced_arguments_pass_like_the_executor_sees_them(stack):
    """`path: []` from a fine-tune reaches the executor as "" (server_stream
    coercion), so the call runs and the run passes."""
    stack.replies = [call_reply("ws_list_directory", {"path": []})]

    assert tool_check.run_check(KEY)["state"] == "passed"


def test_failure_after_passing_runs_reports_how_far_it_got(stack):
    stack.replies = [LIST_ROOT, LIST_ROOT, text_reply("Here are the bugs I found.")]

    result = tool_check.run_check(KEY)

    assert (result["state"], result["passed"], result["runs"]) == ("failed", 2, 3)


def test_a_model_that_does_not_load_records_an_error(stack):
    stack.load_error = "llama-server exited during load"

    result = tool_check.run_check(KEY)

    assert result["state"] == "error"
    assert "llama-server exited during load" in result["reason"]
    assert stack.requests == []


# ── holding the model ────────────────────────────────────────────────────


def test_check_holds_the_model_and_frees_one_it_loaded(stack):
    tool_check.run_check(KEY)

    assert stack.busy == stack.ended == 1
    assert stack.stopped == [KEY]


def test_check_leaves_a_model_that_was_already_loaded(stack):
    stack.resident = KEY

    tool_check.run_check(KEY)

    assert stack.ended == 1
    assert stack.stopped == []


def test_live_state_is_visible_while_running_and_cleared_after(stack, monkeypatch):
    seen = []
    real_one_run = tool_check._one_run

    async def spy(adapter, tools, core_count):
        seen.append(tool_check.result_of(KEY))
        return await real_one_run(adapter, tools, core_count)

    monkeypatch.setattr(tool_check, "_one_run", spy)

    tool_check.run_check(KEY)

    assert [s["run"] for s in seen] == list(range(1, tool_check.RUNS + 1))
    assert all(s["state"] == "running" for s in seen)
    assert not tool_check.is_running(KEY)


# ── when it runs ─────────────────────────────────────────────────────────


class _DoneProc:
    pid = 4242

    def __init__(self, rc):
        self.rc = rc

    def poll(self):
        return self.rc


def _finished_job(key, rc, cancelled=False):
    job = manager._Job(key, _DoneProc(rc), "/dev/null")
    job.cancelled = cancelled
    manager._jobs[key] = job


@pytest.fixture()
def launched(stack, monkeypatch):
    keys: list[str] = []
    monkeypatch.setattr(tool_check, "_launch", keys.append)
    return keys


def test_a_finished_local_chat_download_starts_the_check(launched):
    _finished_job(KEY, 0)

    manager.reap()

    assert launched == [KEY]
    assert tool_check.result_of(KEY)["state"] == "running"


@pytest.mark.parametrize("rc, cancelled", [(1, False), (-15, True)])
def test_a_failed_or_cancelled_download_starts_no_check(launched, rc, cancelled):
    _finished_job(KEY, rc, cancelled)

    manager.reap()

    assert launched == []


def test_other_model_groups_get_no_check(launched):
    _finished_job("whisper_large_v3", 0)

    manager.reap()

    assert launched == []


def test_a_model_without_tools_gets_no_check(launched, monkeypatch):
    from server.local import runtime

    monkeypatch.setattr(runtime, "supports_tools", lambda key: False)

    assert tool_check.schedule(KEY) is False
    assert launched == []


def test_one_check_at_a_time_per_model(launched):
    assert tool_check.schedule(KEY) is True
    assert tool_check.schedule(KEY) is False
    assert launched == [KEY]


# ── delete and the HTTP surface ──────────────────────────────────────────


def _install(tmp_home) -> None:
    entry = get_entry(KEY)
    path = tmp_home / "models" / entry.dir_name / entry.sentinel_rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 64)


def test_delete_waits_for_a_running_check(stack, launched, tmp_path):
    _install(tmp_path)
    tool_check.schedule(KEY)

    with pytest.raises(manager.ModelBusy, match="checked for tool calls"):
        manager.delete(get_entry(KEY))


def test_delete_forgets_the_result(stack, tmp_path):
    _install(tmp_path)
    tool_check.run_check(KEY)
    assert tool_check.result_of(KEY) is not None

    manager.delete(get_entry(KEY))

    assert tool_check.result_of(KEY) is None


def test_catalog_and_status_carry_the_result_for_local_chat_only(stack):
    tool_check.run_check(KEY)
    app = FastAPI()
    app.include_router(router)
    http = TestClient(app)

    catalog = {m["key"]: m for m in http.get("/api/models/catalog").json()["models"]}
    status = http.get("/api/models/status").json()["models"]

    assert catalog[KEY]["tool_check"]["state"] == "passed"
    assert status[KEY]["tool_check"] == catalog[KEY]["tool_check"]
    assert catalog["whisper_large_v3"]["tool_check"] is None
    assert status["whisper_large_v3"]["tool_check"] is None
