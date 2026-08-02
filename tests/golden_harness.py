"""Golden-frame harness for the chat turn loop.

Drives ``POST /api/chat`` end-to-end with a scripted fake Bedrock client and
captures the exact SSE frame sequence the loop emits. The normalized frames
are pinned as fixtures (tests/golden_fixtures/*.json): they are the acceptance
contract the P2 turn-engine cutover must reproduce frame-for-frame, standing
in for the feature flag the direct-replacement rollout does not have.

Regenerate fixtures intentionally with:

    GOLDEN_RECORD=1 pytest tests/test_engine_golden.py

Normalization scrubs the values that legitimately vary by machine (pricing
lookups, live tool-catalog size) while pinning frame ORDER and SHAPE.
"""

import json
import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

# Deterministic executor registry: the app imports every executor module at
# startup; tests import modules piecemeal, so whether analyze_document (etc.)
# is registered would otherwise depend on which tests ran first in the
# session — making the pinned skill_result frames order-dependent.
import server.executors.content  # noqa: F401 — registers analyze_document et al.

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "golden_fixtures")

# ── Scripted Bedrock stand-ins ────────────────────────────────────────────────


class FakeBedrockClient:
    """Serves one scripted Anthropic event stream per invoke call, in order.

    ``rounds`` is a list of lists of Anthropic stream events (dicts). A raised
    RuntimeError on exhaustion makes an unexpected extra round fail the test
    loudly instead of hanging.
    """

    def __init__(self, rounds: list[list[dict]]):
        self._rounds = list(rounds)
        self.requests: list[dict] = []  # parsed request bodies, for assertions

    def invoke_model_with_response_stream(self, modelId, contentType, accept, body):
        self.requests.append(json.loads(body))
        if not self._rounds:
            raise RuntimeError("FakeBedrockClient: no scripted rounds left")
        events = self._rounds.pop(0)
        chunks = []
        for ev in events:
            if "type" in ev:
                chunks.append({"chunk": {"bytes": json.dumps(ev).encode()}})
            else:
                # Transport-level event (mid-stream error), delivered outside
                # the chunk envelope exactly as Bedrock does.
                chunks.append(ev)
        return {"body": iter(chunks)}

    def invoke_model(self, modelId, contentType, accept, body):
        # Compaction summarizer path; deterministic canned summary.
        return {
            "body": _Readable(
                json.dumps({"content": [{"type": "text", "text": "(scripted summary)"}]})
            )
        }


class _Readable:
    def __init__(self, text: str):
        self._text = text

    def read(self):
        return self._text


# ── Event script builders (Anthropic stream shapes) ──────────────────────────


def msg_start(input_tokens=100, cache_read=0, cache_creation=0):
    return {
        "type": "message_start",
        "message": {
            "usage": {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            }
        },
    }


def text_block(text, index=0):
    return [
        {"type": "content_block_start", "index": index, "content_block": {"type": "text"}},
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        },
        {"type": "content_block_stop", "index": index},
    ]


def thinking_block(text, index=0):
    return [
        {"type": "content_block_start", "index": index, "content_block": {"type": "thinking"}},
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "thinking_delta", "thinking": text},
        },
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "signature_delta", "signature": "sig=="},
        },
        {"type": "content_block_stop", "index": index},
    ]


def tool_use_block(tool_id, name, tool_input, index=0):
    return [
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "tool_use", "id": tool_id, "name": name},
        },
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": json.dumps(tool_input)},
        },
        {"type": "content_block_stop", "index": index},
    ]


def msg_end(stop_reason="end_turn", output_tokens=50):
    return [
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason},
            "usage": {"output_tokens": output_tokens},
        },
        {"type": "message_stop"},
    ]


# ── Runner ────────────────────────────────────────────────────────────────────


def run_chat_turn(monkeypatch, fake_client, body):
    """POST /api/chat against a scripted client; return raw SSE data lines."""
    import server.chat.compaction as compaction_mod
    import server.chat.engine.anthropic as engine_anthropic_mod
    import server.chat.routes as routes_mod
    import server.local.route as local_route

    # Patch every module-level binding of the client getter — the route, the
    # compaction summarizer, and the engine's Anthropic adapter. Missing one
    # would silently send the scripted turn to REAL Bedrock.
    if hasattr(routes_mod, "_get_bedrock_client"):
        monkeypatch.setattr(routes_mod, "_get_bedrock_client", lambda: fake_client)
    monkeypatch.setattr(compaction_mod, "_get_bedrock_client", lambda: fake_client)
    monkeypatch.setattr(engine_anthropic_mod, "_get_bedrock_client", lambda: fake_client)
    # Keep the turn on the cloud Anthropic branch regardless of local config.
    monkeypatch.setattr(local_route, "local_chat_response", lambda **kw: None)

    # Hermetic: post-turn fire-and-forget hooks (memory extraction, session
    # memory, dream consolidation, title gen) must not run — they would build
    # their own REAL bedrock clients in the background. Closing the coroutine
    # suppresses the un-awaited warning. Patched at the source module so both
    # the route's own spawns and the engine's post-turn hooks are covered.
    import server.infrastructure.async_tasks as async_tasks_mod

    def _swallow(coro, *, name=None):
        coro.close()

        class _T:
            def cancel(self):
                pass

        return _T()

    monkeypatch.setattr(async_tasks_mod, "spawn", _swallow)
    if hasattr(routes_mod, "spawn"):
        monkeypatch.setattr(routes_mod, "spawn", _swallow)

    routes_mod._active_chat_streams.clear()

    app = FastAPI()
    app.include_router(routes_mod.router)
    client = TestClient(app)

    payload = {
        "question": "hello",
        "transcript": "",
        "history": [],
        "session_id": "golden-session",
        # Deterministic: no index grounding, no attachments, cloud model.
        "selected_search_indexes": [],
        "attachment_ids": [],
        "model": "opus5.0",
        **body,
    }
    with client.stream("POST", "/api/chat", json=payload) as resp:
        assert resp.status_code == 200, resp.read()
        raw = b"".join(resp.iter_raw()).decode()
    lines = [ln[len("data: ") :] for ln in raw.splitlines() if ln.startswith("data: ")]
    return lines


# ── Normalization + fixture comparison ────────────────────────────────────────

_SCRUB_KEYS = {
    # Machine-dependent: pricing files and the live tool catalog vary between
    # the dev checkout and CI. Presence and position stay pinned; values don't.
    "estimated_cost_usd": 0,
}


def _scrub(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _SCRUB_KEYS:
                out[k] = _SCRUB_KEYS[k]
            elif k == "tool_pool" and isinstance(v, dict):
                out[k] = {kk: 0 for kk in v}
            else:
                out[k] = _scrub(v)
        return out
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    return obj


def normalize_frames(lines: list[str]) -> list:
    frames = []
    for ln in lines:
        if ln == "[DONE]":
            frames.append("[DONE]")
            continue
        try:
            frames.append(_scrub(json.loads(ln)))
        except json.JSONDecodeError:
            frames.append(ln)
    return frames


def assert_golden(name: str, lines: list[str]):
    """Compare normalized frames against the stored fixture; regenerate with
    GOLDEN_RECORD=1."""
    frames = normalize_frames(lines)
    path = os.path.join(FIXTURES_DIR, f"{name}.json")
    if os.environ.get("GOLDEN_RECORD"):
        os.makedirs(FIXTURES_DIR, exist_ok=True)
        with open(path, "w") as f:
            json.dump(frames, f, indent=2)
            f.write("\n")
        return
    assert os.path.exists(path), (
        f"Missing golden fixture {name}.json — run GOLDEN_RECORD=1 pytest "
        f"tests/test_engine_golden.py to record it from the current loop."
    )
    with open(path) as f:
        expected = json.load(f)
    assert frames == expected, (
        f"SSE frames diverged from golden fixture {name}.json. If the change "
        f"is intentional, re-record with GOLDEN_RECORD=1."
    )
