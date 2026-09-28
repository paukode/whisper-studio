"""Side-task model calls reach the cost log exactly once, with their source.

The turn engine records chat, agent, workflow, cron, voice and headless
rounds. Titles, compaction summaries, the auto-mode classifier, the
permission explainer, memory recall, query rewriting, the goal evaluator,
index passes, OCR, condensation, the /btw side question and structured-output
distillation call Bedrock themselves; each is billed, so each is logged.
"""

import asyncio
import io
import json
import time
from types import SimpleNamespace

import pytest

from server.chat.infra import _get_chat_models
from server.costs import calls, capture, tracker


class _Body(io.BytesIO):
    pass


class _FakeBedrock:
    """invoke_model answering with a payload that reports its usage."""

    def __init__(self, text="ok", usage=None, delay=0.0):
        self.text = text
        self.usage = usage if usage is not None else {"input_tokens": 321, "output_tokens": 12}
        self.delay = delay
        self.calls = 0

    def invoke_model(self, **kwargs):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        payload = {"content": [{"type": "text", "text": self.text}], "usage": self.usage}
        return {"body": _Body(json.dumps(payload).encode())}


def _only_row(session_id):
    rows = tracker.get_session_costs(session_id)
    assert len(rows) == 1, rows
    return rows[0]


@pytest.fixture(autouse=True)
def _no_real_aws_client(monkeypatch):
    """These tests turn Local mode off, so a code path that reaches a real
    boto3 client would call Bedrock with whatever credentials the machine
    has. Every real client build raises here; each test supplies fakes."""

    def refuse(*a, **k):
        raise AssertionError("a real AWS client was built in a unit test")

    import boto3

    monkeypatch.setattr(boto3.session.Session, "client", refuse)
    monkeypatch.setattr(boto3, "client", refuse)


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setattr("server.infrastructure.cloud_guard.require_cloud", lambda *a, **k: None)
    monkeypatch.setattr("server.infrastructure.cloud_guard.cloud_allowed", lambda *a, **k: True)
    fake = _FakeBedrock()
    # Patched where each caller reads it: routes.py binds its own copy.
    monkeypatch.setattr("server.chat.infra._get_bedrock_client", lambda: fake)
    monkeypatch.setattr("server.chat.routes._get_bedrock_client", lambda: fake)
    return fake


def test_a_cloud_one_shot_is_logged_with_its_source_and_session(cloud):
    from server.infrastructure.oneshot import one_shot

    out = one_shot(
        "sys",
        "user",
        max_tokens=50,
        engine="cloud",
        cloud_model_key="haiku",
        source="compaction",
        session_id="s-compact",
    )
    assert out == "ok"
    row = _only_row("s-compact")
    assert (row["model"], row["source"]) == ("haiku", "compaction")
    assert (row["input_tokens"], row["output_tokens"], row["count_source"]) == (321, 12, "reported")


def test_an_aux_task_is_logged_under_its_source(cloud, monkeypatch):
    from server.infrastructure import auxiliary

    monkeypatch.setattr(auxiliary, "_map", lambda config=None: {})
    auxiliary.aux_one_shot("goal_evaluator", "s", "u", max_tokens=10, session_id="s-goal")
    assert _only_row("s-goal")["source"] == "evaluator"
    assert set(auxiliary.TASK_SOURCES) == set(auxiliary.TASKS)


def test_a_gpt_one_shot_is_logged_from_its_responses_usage(monkeypatch):
    from server.infrastructure import oneshot

    monkeypatch.setattr("server.infrastructure.cloud_guard.require_cloud", lambda *a, **k: None)
    usage = SimpleNamespace(
        input_tokens=900, output_tokens=30, input_tokens_details=SimpleNamespace(cached_tokens=800)
    )
    response = SimpleNamespace(output_text="summary", usage=usage)
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: response))
    monkeypatch.setattr("server.openai_bedrock.runtime.build_sync_client", lambda region: client)
    monkeypatch.setattr("server.openai_bedrock.runtime.region_for", lambda key: "us-east-1")
    gpt_key = next(k for k in _get_chat_models() if k.startswith("gpt"))
    oneshot.one_shot(
        "s",
        "u",
        max_tokens=40,
        engine="cloud",
        cloud_model_key=gpt_key,
        source="compaction",
        session_id="s-gpt",
    )
    row = _only_row("s-gpt")
    assert (row["input_tokens"], row["cache_read_tokens"], row["output_tokens"]) == (900, 800, 30)


def test_the_classifier_is_logged_against_the_session(monkeypatch):
    import boto3

    from server import auto_mode

    fake = _FakeBedrock(text=json.dumps({"decision": "allow", "reason": "read"}))
    monkeypatch.setattr(boto3, "client", lambda *a, **k: fake)
    haiku_id = _get_chat_models()["haiku"]
    config = {"chat_models": {"haiku": haiku_id}}
    asyncio.run(auto_mode.classify_tool_call("Read", {"p": 1}, config, session_id="s-auto"))
    row = _only_row("s-auto")
    assert (row["model"], row["source"]) == ("haiku", "classifier")


def test_an_explainer_call_the_timeout_abandoned_is_still_logged(monkeypatch):
    import boto3

    from server.security import explainer

    fake = _FakeBedrock(text='{"risk": "low"}', delay=0.3)
    monkeypatch.setattr(boto3, "client", lambda *a, **k: fake)
    monkeypatch.setattr(explainer, "_EXPLAINER_TIMEOUT", 0.05)
    haiku_id = _get_chat_models()["haiku"]
    config = {"chat_models": {"haiku": haiku_id}, "permission_explainer_enabled": True}
    monkeypatch.setattr("server.infrastructure.cloud_guard.cloud_allowed", lambda *a, **k: True)
    got = asyncio.run(
        explainer.explain_permission("Bash", {"cmd": "ls"}, [], config, "m", session_id="s-exp")
    )
    assert got is None  # timed out for the caller
    deadline = time.monotonic() + 5
    while not tracker.get_session_costs("s-exp") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _only_row("s-exp")["source"] == "explainer"


def test_a_call_whose_payload_has_no_usage_is_estimated_from_its_payloads(cloud):
    cloud.usage = {}
    body = json.dumps({"messages": [{"role": "user", "content": "x" * 800}]})
    calls.invoke_claude(
        cloud, model_id=_get_chat_models()["haiku"], body=body, source="index", session_id="s-est"
    )
    row = _only_row("s-est")
    assert row["input_tokens"] == capture.estimate_tokens(len(body))
    assert row["output_tokens"] == capture.estimate_tokens(len("ok"))
    assert (row["count_source"], row["estimated_fields"]) == ("estimated", "input,output")


def test_an_unknown_source_is_refused_before_the_call(cloud):
    with pytest.raises(ValueError):
        calls.invoke_claude(cloud, model_id="m", body="{}", source="mystery")
    assert cloud.calls == 0


def _btw_events():
    return [
        {"type": "message_start", "message": {"usage": {"input_tokens": 40, "output_tokens": 1}}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "An answer."}},
        {"type": "message_delta", "usage": {"output_tokens": 7}},
        {"type": "message_stop"},
    ]


def test_a_side_question_stream_is_logged_once_complete_or_abandoned():
    haiku_id = _get_chat_models()["haiku"]
    done = calls.ClaudeStreamRecorder(model_id=haiku_id, body="{}", source="btw", session_id="b1")
    for ev in _btw_events():
        done.observe(ev)
        done.received(ev.get("delta", {}).get("text", ""))
    done.finish()
    done.finish()  # idempotent
    row = _only_row("b1")
    assert (row["input_tokens"], row["output_tokens"], row["source"]) == (40, 7, "btw")

    cut = calls.ClaudeStreamRecorder(model_id=haiku_id, body="{}", source="btw", session_id="b2")
    for ev in _btw_events()[:2]:
        cut.observe(ev)
        cut.received(ev.get("delta", {}).get("text", ""))
    cut.finish()
    row = _only_row("b2")
    assert row["input_tokens"] == 40 and row["estimated_fields"] == "output"
    assert row["output_tokens"] == capture.estimate_tokens(len("An answer."))


def test_the_title_endpoint_logs_the_call_against_the_session(cloud):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.chat import routes

    cloud.text = "A Title"
    app = FastAPI()
    app.include_router(routes.router)
    r = TestClient(app).post(
        "/api/generate-title", json={"text": "User: hi", "session_id": "s-title"}
    )
    assert r.status_code == 200
    assert _only_row("s-title")["source"] == "title"


def _distill(adapter, session_id, schema=None):
    from server.agents.providers.base import TurnUsage
    from server.agents.runtime_support import _distill_structured

    total = TurnUsage()
    out = asyncio.run(
        _distill_structured(
            adapter,
            "sys",
            [{"role": "user", "content": "task"}],
            schema or {"type": "object"},
            SimpleNamespace(max_tokens=100),
            total,
            session_id=session_id,
            model_key="opus5.0",
            source="workflow",
        )
    )
    return out, total


class _DistillBedrock:
    """invoke_model answering with emit_result payloads, each with its own
    usage (or none) and Bedrock token-count headers (or none)."""

    def __init__(self, replies):
        self.replies, self.bodies = list(replies), []

    def invoke_model(self, **kwargs):
        self.bodies.append(kwargs["body"])
        result, usage, headers = self.replies.pop(0)
        payload = {"content": [{"type": "tool_use", "name": "emit_result", "input": result}]}
        if usage is not None:
            payload["usage"] = usage
        return {
            "body": _Body(json.dumps(payload).encode()),
            "ResponseMetadata": {"HTTPHeaders": headers or {}},
        }


def test_structured_output_distillation_is_logged_per_attempt_under_the_runs_source():
    from server.agents.providers.anthropic import AnthropicBedrockAdapter

    adapter = AnthropicBedrockAdapter(model_key="opus5.0", model_id="m")
    adapter._bedrock = _DistillBedrock(
        [
            # Fails the schema, so a repair attempt follows. Bedrock's own
            # metering disagrees with the payload's usage and wins.
            (
                {"ok": "yes"},
                {"input_tokens": 500, "output_tokens": 20},
                {"x-amzn-bedrock-input-token-count": "530"},
            ),
            ({"ok": True}, None, None),  # no count anywhere: characters / 4
        ]
    )
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    out, total = _distill(adapter, "s-distill", schema)
    assert out == {"ok": True}
    first, second = tracker.get_session_costs("s-distill")
    assert (first["source"], first["model"], first["input_tokens"]) == ("workflow", "opus5.0", 530)
    assert first["count_source"] == "reported" and first["count_detail"]
    posted = adapter._bedrock.bodies[1]
    assert second["input_tokens"] == capture.estimate_tokens(len(posted))
    assert second["output_tokens"] == capture.estimate_tokens(len('{"ok":true}'))
    assert (second["count_source"], second["estimated_fields"]) == ("estimated", "input,output")
    assert total.input_tokens == 530 + second["input_tokens"]


class _GptDistillStream:
    def __init__(self, events, error=None):
        self.events, self.error = events, error

    def __aiter__(self):
        async def gen():
            for e in self.events:
                yield e
            if self.error is not None:
                raise self.error

        return gen()


def _gpt_distill_adapter(monkeypatch, stream):
    import server.openai_bedrock.runtime as oai
    from server.agents.providers.openai import OpenAIBedrockAdapter

    posted = []

    async def create(**kwargs):
        posted.append(kwargs)
        return stream

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    monkeypatch.setattr(oai, "build_client", lambda region: client)
    monkeypatch.setattr(oai, "region_for", lambda mk: "us-east-2")
    return OpenAIBedrockAdapter(model_key="opus5.0", model_id="openai.gpt"), posted


def test_a_gpt_distillation_that_ends_without_usage_is_estimated(monkeypatch):
    answer = '{"ok": true}'
    delta = SimpleNamespace(type="response.output_text.delta", delta=answer)
    adapter, posted = _gpt_distill_adapter(monkeypatch, _GptDistillStream([delta]))
    out, _total = _distill(adapter, "s-gpt-distill")
    assert out == {"ok": True}
    row = _only_row("s-gpt-distill")
    # response.completed never came, so no usage: never a reported zero.
    assert row["input_tokens"] == capture.estimate_tokens(capture.json_chars(posted[0]))
    assert row["output_tokens"] == capture.estimate_tokens(len(answer))
    assert (row["count_source"], row["estimated_fields"]) == ("estimated", "input,output")


def test_a_truncated_gpt_distillation_takes_its_counts_from_the_payload(monkeypatch):
    answer = '{"ok": true}'
    usage = SimpleNamespace(
        input_tokens=9000, output_tokens=4000, input_tokens_details=SimpleNamespace(cached_tokens=0)
    )
    events = [
        SimpleNamespace(type="response.output_text.delta", delta=answer),
        SimpleNamespace(type="response.incomplete", response=SimpleNamespace(usage=usage)),
    ]
    adapter, _posted = _gpt_distill_adapter(monkeypatch, _GptDistillStream(events))
    _distill(adapter, "s-gpt-trunc")
    row = _only_row("s-gpt-trunc")
    assert (row["input_tokens"], row["output_tokens"]) == (9000, 4000)
    assert row["count_source"] == "reported"


def test_a_gpt_distillation_that_breaks_mid_stream_is_still_logged(monkeypatch):
    delta = SimpleNamespace(type="response.output_text.delta", delta='{"ok"')
    stream = _GptDistillStream([delta], error=RuntimeError("connection reset"))
    adapter, posted = _gpt_distill_adapter(monkeypatch, stream)
    with pytest.raises(RuntimeError):
        _distill(adapter, "s-gpt-broken")
    row = _only_row("s-gpt-broken")
    assert row["input_tokens"] == capture.estimate_tokens(capture.json_chars(posted[0]))
    assert row["count_source"] == "estimated"


class _Cohere:
    """Bedrock invoke_model for Cohere Embed v4 (with Bedrock's input
    token-count header) and Rerank 3.5 (without one)."""

    def __init__(self):
        self.bodies = []

    def invoke_model(self, modelId, body):
        self.bodies.append(body)
        req = json.loads(body)
        if "texts" in req:
            payload = {"embeddings": {"float": [[1.0, 0.0] for _ in req["texts"]]}}
            headers = {"x-amzn-bedrock-input-token-count": "42"}
        else:
            payload = {"results": [{"index": 1, "relevance_score": 0.9}]}
            headers = {}
        return {
            "body": _Body(json.dumps(payload).encode()),
            "ResponseMetadata": {"HTTPHeaders": headers},
        }


def test_cohere_embed_is_priced_and_rerank_is_shown_as_unpriced(monkeypatch):
    from datetime import datetime, timezone

    from server.costs import usage
    from server.index import embedder_cohere, reranker
    from server.index.config import COHERE_EMBED_MODEL_ID, COHERE_RERANK_MODEL_ID

    fake = _Cohere()
    monkeypatch.setattr(embedder_cohere, "_bedrock", lambda: fake)
    embedder_cohere.embed_documents(["alpha", "beta"])
    assert reranker._rerank_cohere("q", ["alpha", "beta"]) == [0.0, 0.9]
    embed, rerank = tracker.get_session_costs("")
    assert (embed["model"], embed["source"], embed["input_tokens"]) == (
        COHERE_EMBED_MODEL_ID,
        "index",
        42,
    )
    assert (embed["output_tokens"], embed["count_source"]) == (0, "reported")
    assert rerank["model"] == COHERE_RERANK_MODEL_ID
    assert rerank["input_tokens"] == capture.estimate_tokens(len(fake.bodies[1]))
    assert (rerank["count_source"], rerank["estimated_fields"]) == ("estimated", "input")
    # Embed bills per input token at the Price List rate. Rerank bills per
    # search unit, so its token row has no rate and says so.
    today = datetime.now(timezone.utc).date()
    rows = {r["key"]: r for r in usage.usage_report(today, today, "day", "model")["rows"]}
    embed_rate = tracker.get_model_pricing(COHERE_EMBED_MODEL_ID)["input"]
    assert rows[COHERE_EMBED_MODEL_ID]["cost_usd"] == pytest.approx(42 * embed_rate / 1e6, abs=1e-6)
    assert rows[COHERE_EMBED_MODEL_ID]["cost_usd"] > 0
    assert rows[COHERE_EMBED_MODEL_ID]["note"] == ""
    assert rows[COHERE_RERANK_MODEL_ID]["note"] == usage.UNPRICED_NOTE
