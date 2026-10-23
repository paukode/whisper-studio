"""Tool-call check for a freshly downloaded on-device chat model.

A local model can load and chat fine and still never produce a tool call that
llama-server parses: it writes the call as JSON in a code block, or as text in
its own dialect, or stops after announcing what it will do. Every turn then
ends after one sentence and the user sees a model that "does not work". Both
failures were seen on one Mac (2026-10-20): Qwen2.5-Coder-7B wrote its calls
as JSON text, and a Gemma 4 coder fine-tune called a tool on about half of its
first turns and never on a follow-up, while the official Gemma 4 build called
one on 15 of 15 replays of the same requests.

So after a local-chat download finishes, the model is loaded the way a chat
turn loads it and sent a real first turn ``RUNS`` times: the local system
prompt with a workspace connected, the tool pool a fresh chat session
advertises, and a request that needs a tool. Each run goes through the real
LocalAdapter round (the same request body, stream parser, empty-completion
retry and argument coercion as chat), and passes when it ends in a call to a
declared tool with arguments of the declared types. No tool ever runs. The
check stops at the first failed run: one dead turn in five is already the
answer the user needs.

The result is stored per model key under the data root and shown on the
model's row in Settings > Models. The check holds the model like a chat turn
does (``mark_busy``), so a switch to another model waits for it, and it never
displaces a model while a local turn streams from it (``ensure_serving``
waits). When the checked model was not the loaded one, it is stopped again
afterwards, so a download never leaves several GB resident.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time

from server.infrastructure.paths import data_root

log = logging.getLogger("whisper-studio")

# Runs per check. The warning threshold is "any failure": at 4 of 5, one turn
# in five still ends without doing anything.
RUNS = 5

# The user's own words from the session where two models failed: a request
# that needs the workspace tools to answer.
PROMPT = "Check the open workspace code, and tell me what bugs are there?"

# Only text in the system prompt: no tool runs, so the folder is never read.
_EXAMPLE_WORKSPACE = os.path.join(os.path.expanduser("~"), "Projects", "example-app")

_STORE_NAME = "local-tool-checks.json"

_lock = threading.Lock()
# key -> live state of a running check ({"state": "running", ...}).
_live: dict[str, dict] = {}


def _store_path() -> str:
    return os.path.join(data_root(), _STORE_NAME)


def _read_store() -> dict:
    try:
        with open(_store_path()) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("Could not read the tool-call check results: %s", e)
        return {}
    return data if isinstance(data, dict) else {}


def _write_store(data: dict) -> None:
    path = _store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def result_of(key: str) -> dict | None:
    """The live state of a running check, else the stored result, else None."""
    with _lock:
        live = _live.get(key)
        if live is not None:
            return dict(live)
        stored = _read_store().get(key)
    return stored if isinstance(stored, dict) else None


def is_running(key: str) -> bool:
    with _lock:
        return key in _live


def forget(key: str) -> None:
    """Drop the stored result of a model that left the disk."""
    with _lock:
        data = _read_store()
        if key in data:
            del data[key]
            _write_store(data)


def _set_live(key: str, **state) -> None:
    with _lock:
        _live[key] = {"state": "running", "total": RUNS, **state}


def _finish(key: str, result: dict) -> dict:
    with _lock:
        _live.pop(key, None)
        data = _read_store()
        data[key] = result
        _write_store(data)
    return result


def _start_thread(key: str) -> None:
    threading.Thread(target=_run_logged, args=(key,), name=f"tool-check-{key}", daemon=True).start()


# How schedule() starts a check. Module-level so tests replace it
# (tests/conftest.py refuses live checks: one would load a real model).
_launch = _start_thread


def schedule(key: str) -> bool:
    """Start a check of ``key`` in the background unless one is running or the
    model is not registered as tool-capable (then chat never sends it tools,
    and there is nothing to check). Returns whether a check was started."""
    from server.local import runtime

    if not runtime.supports_tools(key):
        return False
    with _lock:
        if key in _live:
            return False
        _live[key] = {"state": "running", "total": RUNS, "run": 0, "passed": 0}
    try:
        _launch(key)
    except Exception:
        with _lock:
            _live.pop(key, None)
        raise
    return True


def _run_logged(key: str) -> None:
    try:
        result = run_check(key)
    except Exception as e:  # noqa: BLE001 - a background check must record, not raise
        log.exception("Tool-call check of %s failed to run.", key)
        result = _finish(key, _result("error", 0, 0, f"Unexpected error: {e}"))
    log.info(
        "Tool-call check of %s: %s (%d of %d passed). %s",
        key,
        result["state"],
        result["passed"],
        result["total"],
        result.get("reason") or "",
    )


def _result(state: str, passed: int, runs: int, reason: str = "") -> dict:
    return {
        "state": state,
        "passed": passed,
        "runs": runs,
        "total": RUNS,
        "reason": reason,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def fresh_session_tools() -> tuple[list[dict], int | None]:
    """The tools a chat turn advertises on its first round in a new session
    with a workspace connected (runner._assemble_round_tools for a session
    with no tool_search activations)."""
    from server.chat.tool_partition import partition_pool
    from server.chat.tool_pool import assemble_full_catalog
    from server.infrastructure.feature_flags import is_enabled

    catalog = assemble_full_catalog(ws_connected=True)
    if is_enabled("progressive_tools"):
        tools, _, core_count = partition_pool(catalog, [])
        return tools, core_count
    return catalog, None


_JSON_TYPES = {
    "string": str,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def _type_ok(value, want) -> bool:
    if isinstance(want, list):
        return any(_type_ok(value, w) for w in want)
    if want == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if want == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if want == "null":
        return value is None
    py = _JSON_TYPES.get(want)
    return py is None or isinstance(value, py)


def call_problem(call, tools_by_name: dict[str, dict]) -> str:
    """Why ``call`` would not run as the model meant it, or '' when it would:
    the tool must be declared, its required arguments present, and every
    argument it sends of the declared JSON type (after the adapter's
    coercion, which is what the executor receives)."""
    tool = tools_by_name.get(call.name)
    if tool is None:
        return f"called a tool that was not offered ({call.name})"
    schema = tool.get("input_schema") or {}
    props = schema.get("properties") or {}
    args = call.input if isinstance(call.input, dict) else {}
    missing = [r for r in schema.get("required") or [] if r not in args]
    if missing:
        return f"called {call.name} without {', '.join(missing)}"
    for name, value in args.items():
        want = (props.get(name) or {}).get("type")
        if want and not _type_ok(value, want):
            return f"called {call.name} with {name} of the wrong type"
    return ""


async def _one_run(adapter, tools: list[dict], core_count) -> str:
    """One first-turn round. Returns '' on a pass, else why it failed."""
    from server.chat.engine.events import RoundError, RoundResult, ToolCall

    tools_by_name = {t["name"]: t for t in tools}
    messages = [{"role": "user", "content": PROMPT}]
    calls: list = []
    stop_reason = ""
    async for ev in adapter.stream_round(messages, tools, core_count, 0, False):
        if isinstance(ev, ToolCall):
            calls.append(ev)
        elif isinstance(ev, RoundError):
            return f"could not answer ({ev.message})"
        elif isinstance(ev, RoundResult):
            stop_reason = ev.stop_reason
    if not calls:
        if stop_reason == "max_tokens":
            return "reached its output limit before it called a tool"
        return "answered without calling a tool"
    problems = [p for p in (call_problem(c, tools_by_name) for c in calls) if p]
    return problems[0] if problems else ""


def run_check(key: str) -> dict:
    """Load ``key``, run the check, store and return the result. Blocking:
    a cold load and the first prompt take minutes on an 18 GB Mac."""
    from server.chat.engine.local import LocalAdapter
    from server.local import runtime, serving

    _set_live(key, run=0, passed=0)
    was_resident = serving.resident_key() == key
    try:
        base_url = serving.ensure_serving(key, runtime.requested_n_ctx(), mark_busy=True)
    except Exception as e:  # noqa: BLE001 - recorded for the user, not raised
        return _finish(key, _result("error", 0, 0, f"Could not load the model: {e}"))
    passed = 0
    attempted = 0
    reason = ""
    try:
        adapter = LocalAdapter(
            model_key=key,
            base_url=base_url,
            system_prompt=runtime.build_local_system_prompt(tools=True, ws_path=_EXAMPLE_WORKSPACE),
            thinking=runtime.supports_thinking(key),
            tools_enabled=True,
            wire_model=serving.wire_model(key),
            supports_tool_choice=serving.supports_tool_choice(key),
        )
        tools, core_count = fresh_session_tools()
        for i in range(RUNS):
            attempted = i + 1
            _set_live(key, run=attempted, passed=passed)
            reason = asyncio.run(_one_run(adapter, tools, core_count))
            if reason:
                break
            passed += 1
    finally:
        serving.end_turn()
        if not was_resident:
            serving.stop_if_idle(key)
    state = "passed" if passed == RUNS else "failed"
    return _finish(key, _result(state, passed, attempted, reason))
