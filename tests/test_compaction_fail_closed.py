"""Mock-only contracts for atomic compaction and strict one-shot responses."""

import asyncio
import io
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

# Import before any configuration fixture, not under a patched load_config.
import server.openai_bedrock.runtime  # noqa: F401
from server.chat import compaction, compaction_log, infra
from server.infrastructure import config, oneshot


@pytest.fixture(autouse=True)
def models(monkeypatch):
    cfg = {
        "model_mode": "cloud",
        "chat_models": {
            "claude": "global.anthropic.claude-haiku-4-5",
            "gpt": "openai.gpt-5.5",
            "local_test": "local:test",
        },
        "chat_model_meta": {"gpt": {"provider": "openai_bedrock"}},
    }
    monkeypatch.setattr(config, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(infra, "_get_chat_models", lambda: cfg["chat_models"])
    monkeypatch.setattr(infra, "_get_chat_model_meta", lambda: cfg["chat_model_meta"])
    monkeypatch.setattr("server.local.runtime.is_local_model", lambda k: k == "local_test")
    monkeypatch.setattr(compaction, "thresholds_for", lambda *a, **k: (100_000, 120_000))
    monkeypatch.setattr("server.infrastructure.feature_flags.is_enabled", lambda *_: True)
    return cfg


def history():
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i}: " + "x" * 3000}
        for i in range(14)
    ]


def compact(messages, trigger="context-overflow"):
    return asyncio.run(
        compaction.compact_messages_with_claude(
            messages, "unused-id", session_id="test-session", model_key="claude", trigger=trigger
        )
    )


@pytest.mark.parametrize(
    "auxiliary",
    [
        None,
        [],
        "claude",
        7,
        {"compaction": None},
        {"compaction": []},
        {"compaction": " "},
        {"compaction": "missing"},
        {"compaction": " claude "},
        {"compaction": False},
    ],
)
def test_bad_compaction_setting_refuses_without_call(models, monkeypatch, auxiliary):
    models["auxiliary_models"] = auxiliary
    call = Mock(side_effect=AssertionError("must reject configuration before model call"))
    monkeypatch.setattr(oneshot, "one_shot", call)
    messages = history()
    original = deepcopy(messages)
    with pytest.raises(compaction.CompactionError):
        compact(messages)
    assert messages == original
    call.assert_not_called()


@pytest.mark.parametrize("auxiliary", [{}, {"compaction": "main"}, {"compaction": "gpt"}])
def test_missing_or_explicit_compaction_setting(models, auxiliary):
    models["auxiliary_models"] = auxiliary
    assert compaction._summary_model_key("claude") == (
        "gpt" if auxiliary.get("compaction") == "gpt" else "claude"
    )


def test_config_read_failure_refuses(monkeypatch):
    monkeypatch.setattr(config, "load_config", Mock(side_effect=OSError("private config data")))
    with pytest.raises(compaction.CompactionError) as error:
        compact(history())
    assert "private config data" not in str(error.value)


@pytest.mark.parametrize("count", [0, 2, 5, 8])
def test_short_overflow_refuses_ordinary_pressure_is_unchanged(count):
    messages = history()[:count]
    original = deepcopy(messages)
    with pytest.raises(compaction.CompactionError):
        compact(messages)
    assert compact(messages, "pressure") == original
    assert messages == original


def long_turn(result_chars=6000, pairs=10):
    """One prompt and many tool rounds: the shape of a field session whose
    compaction found no clean user turn to cut at and changed nothing."""
    messages = [{"role": "user", "content": "begin the audit"}]
    for i in range(pairs):
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": str(i), "name": "read", "input": {}}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": str(i),
                            "content": "x" * result_chars,
                        }
                    ],
                },
            ]
        )
    return messages


def test_one_long_turn_prunes_its_old_tool_results_under_pressure():
    messages = long_turn()
    original = deepcopy(messages)
    out = compact(messages, "pressure")
    assert messages == original
    assert len(out) == len(messages)
    assert out[0] == messages[0]
    assert out[-compaction.KEEP_RECENT :] == messages[-compaction.KEEP_RECENT :]
    assert compaction.estimate_message_size(out) < compaction.estimate_message_size(messages)
    assert compaction.sanitize_tool_pairs(out) == out


def test_one_long_turn_is_summarized_inside_the_turn_keeping_the_prompt(monkeypatch):
    call = Mock(return_value="Short summary")
    monkeypatch.setattr(oneshot, "one_shot", call)
    messages = long_turn()
    original = deepcopy(messages)
    # An overflow skips pruning; before this change it raised for want of a cut.
    out = compact(messages)
    assert messages == original
    assert call.call_count == 1
    first, kept = out[0], out[1:]
    assert compaction._is_clean_user_start(first)
    assert first["content"].startswith("begin the audit\n\n" + compaction.SUMMARY_HEADER)
    assert "Short summary" in first["content"]
    # The prompt is kept once, not again among the summary's verbatim excerpts.
    assert first["content"].count("begin the audit") == 1
    assert len(kept) >= compaction.KEEP_RECENT
    assert kept == messages[-len(kept) :]
    assert kept[0]["role"] == "assistant"
    assert compaction.sanitize_tool_pairs(out) == out


def test_a_turn_too_large_for_the_summary_model_is_summarized_from_its_pruned_part(
    models, monkeypatch
):
    models["auxiliary_models"] = {"compaction": "gpt"}
    monkeypatch.setattr(
        compaction,
        "thresholds_for",
        lambda key: (100_000, 120_000) if key == "claude" else (30_000, 40_000),
    )
    call = Mock(return_value="Short summary")
    monkeypatch.setattr(oneshot, "one_shot", call)
    messages = long_turn(result_chars=8000)
    out = compact(messages)
    assert call.call_count == 1
    prompt = call.call_args.args[1]
    assert "x" * 8000 not in prompt  # the old part was pruned to fit
    assert "removed for compaction" in prompt
    assert out[0]["content"].startswith("begin the audit")


def test_a_cut_never_splits_a_tool_pair_or_starts_on_a_result():
    messages = long_turn()
    split, turn = compaction._summary_cut(messages, compaction.KEEP_RECENT)
    assert turn == 0
    assert messages[split]["role"] == "assistant"
    assert len(messages) - split >= compaction.KEEP_RECENT
    # A history with a clean user turn near the end still cuts there.
    later = [*messages, {"role": "user", "content": "next"}, *long_turn(pairs=5)[1:]]
    split, turn = compaction._summary_cut(later, compaction.KEEP_RECENT)
    assert later[split]["role"] == "assistant" and turn == len(messages)


def test_list_content_prompt_keeps_its_blocks_and_gets_a_summary_block():
    prompt = {"role": "user", "content": [{"type": "text", "text": "look"}]}
    out = compaction._prompt_with_summary(prompt, "S")
    assert out["content"] == [{"type": "text", "text": "look"}, {"type": "text", "text": "S"}]
    assert prompt["content"] == [{"type": "text", "text": "look"}]


@pytest.mark.parametrize("summary", ["", " \n\t", "x" * 100_000, None])
def test_empty_or_no_progress_summary_is_atomic(monkeypatch, summary):
    call = Mock(return_value=summary)
    monkeypatch.setattr(oneshot, "one_shot", call)
    messages = history()
    original = deepcopy(messages)
    with pytest.raises(compaction.CompactionError):
        compact(messages)
    assert messages == original
    assert call.call_count == 1


def test_smaller_but_still_over_hard_ceiling_is_rejected(models, monkeypatch):
    models["auxiliary_models"] = {"compaction": "gpt"}
    monkeypatch.setattr(
        compaction,
        "thresholds_for",
        lambda key: (10_000, 20_000) if key == "claude" else (100_000, 120_000),
    )
    call = Mock(return_value="Short summary")
    monkeypatch.setattr(oneshot, "one_shot", call)
    messages = history()
    original = deepcopy(messages)
    with pytest.raises(compaction.CompactionError):
        compact(messages)
    assert call.call_count == 1
    assert messages == original


def test_oversize_summary_input_refuses_before_call(models, monkeypatch):
    models["auxiliary_models"] = {"compaction": "gpt"}
    monkeypatch.setattr(
        compaction,
        "thresholds_for",
        lambda key: (100_000, 120_000) if key == "claude" else (1000, 2000),
    )
    call = Mock(return_value="must not be used")
    monkeypatch.setattr(oneshot, "one_shot", call)
    messages = history()
    original = deepcopy(messages)
    with pytest.raises(compaction.CompactionError):
        compact(messages)
    assert messages == original
    call.assert_not_called()


def test_full_text_tools_and_attachment_references_reach_one_summary(monkeypatch):
    messages = history()
    messages[0]["content"] += "TAIL_USER_SENTINEL"
    messages[1]["content"] = [
        {"type": "tool_use", "id": "t1", "name": "read", "input": {"z": "arg", "a": "ARG_SENTINEL"}}
    ]
    messages[2]["content"] = [
        {
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": [
                {"type": "text", "text": "x" * 5000 + "RESULT_SENTINEL"},
                {
                    "type": "image",
                    "attachment_id": "attachment-123",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "BINARY_SENTINEL",
                    },
                },
            ],
        }
    ]
    # A pruneable result's tail must also reach the summary, not only nested results.
    messages[4]["content"] = [
        {"type": "tool_result", "tool_use_id": "old", "content": "x" * 5000 + "STRING_RESULT_TAIL"}
    ]
    original = deepcopy(messages)
    call = Mock(return_value="Current summary")
    monkeypatch.setattr(oneshot, "one_shot", call)
    out = compact(messages)
    prompt = call.call_args.args[1]
    for sentinel in (
        "TAIL_USER_SENTINEL",
        "ARG_SENTINEL",
        "RESULT_SENTINEL",
        "STRING_RESULT_TAIL",
        "attachment-123",
    ):
        assert sentinel in prompt
    assert "BINARY_SENTINEL" not in prompt
    assert (
        "binary attachment omitted; reference=history[2].content[0].content[1].source.data; sha256="
        in prompt
    )
    assert out[1:] == original[6:]
    assert messages == original
    assert compaction.estimate_message_size(out) < compaction.estimate_message_size(messages)
    assert call.call_count == 1
    assert call.call_args.kwargs["max_tokens"] == 1024
    assert call.call_args.kwargs["source"] == "compaction"
    assert call.call_args.kwargs["session_id"] == "test-session"


def test_extraction_failure_is_not_success(monkeypatch):
    monkeypatch.setattr(
        "server.chat.compaction_anchors.render_summary_extras",
        Mock(side_effect=ValueError("bad extractor")),
    )
    call = Mock(return_value="summary")
    monkeypatch.setattr(oneshot, "one_shot", call)
    messages = history()
    original = deepcopy(messages)
    with pytest.raises(compaction.CompactionError):
        compact(messages)
    assert messages == original
    call.assert_not_called()


def test_cancellation_closes_bracket_and_keeps_input(monkeypatch):
    records = []
    monkeypatch.setattr(
        compaction_log, "record_start", lambda *a, **k: records.append(("start", k))
    )
    monkeypatch.setattr(compaction_log, "record_end", lambda *a, **k: records.append(("end", k)))
    messages = history()
    original = deepcopy(messages)

    async def scenario():
        loop = asyncio.get_running_loop()
        pending = loop.create_future()
        calls = []

        def submit(*args):
            calls.append(args)
            loop.call_soon(task.cancel)
            return pending

        monkeypatch.setattr(loop, "run_in_executor", submit)
        task = asyncio.create_task(
            compaction.compact_messages_with_claude(
                messages, "unused", model_key="claude", trigger="context-overflow"
            )
        )
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(calls) == 1

    asyncio.run(scenario())
    assert messages == original
    assert [r[0] for r in records] == ["start", "end"]
    assert records[-1][1]["outcome"] == "error"


def test_telemetry_failure_does_not_mask_original_error(monkeypatch):
    original_error = compaction.CompactionError()
    monkeypatch.setattr(compaction_log, "record_start", Mock(side_effect=OSError("log broken")))
    monkeypatch.setattr(compaction_log, "record_end", Mock(side_effect=OSError("log broken")))
    monkeypatch.setattr(oneshot, "one_shot", Mock(side_effect=original_error))
    with pytest.raises(compaction.CompactionError) as caught:
        compact(history())
    assert caught.value is original_error


@pytest.mark.parametrize(
    "key, model_id",
    [
        ("missing", None),
        ("unsupported", "cohere.command-r-v1:0"),
        ("fable", "global.anthropic.claude-fable-5"),
        ("local_cloud", "local:test"),
    ],
)
def test_unknown_or_unsupported_cloud_key_never_constructs_client(
    models, monkeypatch, key, model_id
):
    if model_id:
        models["chat_models"][key] = model_id
    client = Mock(side_effect=AssertionError("no client"))
    monkeypatch.setattr(infra, "_get_bedrock_client", client)
    monkeypatch.setattr("server.openai_bedrock.runtime.build_sync_client", client)
    with pytest.raises(RuntimeError):
        oneshot.one_shot(
            "sys", "usr", max_tokens=100, engine="cloud", cloud_model_key=key, source="compaction"
        )
    client.assert_not_called()


@pytest.mark.parametrize(
    "reason", ["max_tokens", "refusal", "pause_turn", "content_filter", "tool_use"]
)
def test_claude_partial_response_records_cost_before_rejection(monkeypatch, reason):
    payload = {
        "stop_reason": reason,
        "content": [{"type": "text", "text": "partial text"}],
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }
    client = Mock()
    client.invoke_model.return_value = {"body": io.BytesIO(json.dumps(payload).encode())}
    record = Mock()
    monkeypatch.setattr("server.costs.calls.record_invoke", record)
    with pytest.raises(RuntimeError, match="incomplete or refused"):
        oneshot.one_shot(
            "sys",
            "usr",
            max_tokens=100,
            engine="cloud",
            cloud_model_key="claude",
            source="compaction",
            session_id="sid",
            get_bedrock_client=lambda: client,
        )
    assert client.invoke_model.call_count == record.call_count == 1
    assert record.call_args.kwargs["source"] == "compaction"
    assert record.call_args.kwargs["session_id"] == "sid"
    assert json.loads(record.call_args.kwargs["body"])["max_tokens"] == 100


@pytest.mark.parametrize("reason", ["max_output_tokens", "content_filter"])
def test_openai_real_incomplete_shape_logged_before_rejection(monkeypatch, reason):
    from openai.types.responses import Response

    response = Response.model_construct(
        id="resp-test",
        object="response",
        created_at=0,
        model="openai.gpt-5.5",
        status="incomplete",
        incomplete_details={"reason": reason},
        output=[
            {
                "type": "message",
                "id": "msg-test",
                "role": "assistant",
                "status": "incomplete",
                "content": [{"type": "output_text", "text": "partial", "annotations": []}],
            }
        ],
    )
    _assert_openai_rejected(monkeypatch, response)


def _assert_openai_rejected(monkeypatch, response):
    client = Mock()
    client.responses.create.return_value = response
    monkeypatch.setattr("server.openai_bedrock.runtime.build_sync_client", lambda region: client)
    monkeypatch.setattr("server.openai_bedrock.runtime.region_for", lambda key: "us-east-1")
    record = Mock()
    monkeypatch.setattr("server.costs.calls.record_responses", record)
    with pytest.raises(RuntimeError):
        oneshot.one_shot(
            "sys",
            "usr",
            max_tokens=100,
            engine="cloud",
            cloud_model_key="gpt",
            source="compaction",
            session_id="sid",
        )
    assert client.responses.create.call_count == record.call_count == 1
    assert record.call_args.kwargs["session_id"] == "sid"
    assert record.call_args.kwargs["source"] == "compaction"
    assert client.responses.create.call_args.kwargs["store"] is False
    assert client.responses.create.call_args.kwargs["max_output_tokens"] == 100


@pytest.mark.parametrize(
    "output",
    [
        [{"type": "message", "status": "incomplete", "content": []}],
        [
            {
                "type": "message",
                "status": "completed",
                "content": [{"type": "refusal", "refusal": "no"}],
            }
        ],
    ],
)
def test_openai_partial_items_or_refusal_rejected(monkeypatch, output):
    _assert_openai_rejected(
        monkeypatch, SimpleNamespace(status="completed", output=output, output_text="partial")
    )


@pytest.mark.parametrize("provider", ["claude", "gpt", "local_test"])
def test_one_shot_rejects_whitespace(monkeypatch, provider):
    monkeypatch.setattr("server.local.runtime.is_downloaded", lambda key: True)
    monkeypatch.setattr("server.infrastructure.oneshot._local_one_shot", lambda *a, **k: " \n ")
    monkeypatch.setattr(
        "server.costs.calls.invoke_claude",
        lambda *a, **k: {"stop_reason": "end_turn", "content": [{"type": "text", "text": " \n "}]},
    )
    client = Mock()
    client.responses.create.return_value = SimpleNamespace(
        status="completed", output=[], output_text=" \n "
    )
    monkeypatch.setattr("server.openai_bedrock.runtime.build_sync_client", lambda region: client)
    monkeypatch.setattr("server.openai_bedrock.runtime.region_for", lambda key: "us-east-1")
    monkeypatch.setattr("server.costs.calls.record_responses", Mock())
    with pytest.raises(RuntimeError):
        oneshot.one_shot(
            "sys",
            "usr",
            max_tokens=100,
            engine=provider if provider == "local_test" else "cloud",
            cloud_model_key=provider,
            source="compaction",
            get_bedrock_client=lambda: client,
        )


def test_local_missing_explicit_key_or_weights_does_not_substitute(monkeypatch):
    complete = Mock(side_effect=AssertionError("no local call"))
    monkeypatch.setattr("server.infrastructure.oneshot._local_one_shot", complete)
    monkeypatch.setattr("server.local.runtime.is_downloaded", lambda key: False)
    for engine, local_key in [("local_test", None), ("local", "missing")]:
        with pytest.raises(RuntimeError):
            oneshot.one_shot(
                "sys",
                "usr",
                max_tokens=100,
                engine=engine,
                local_model_key=local_key,
                source="compaction",
            )
    complete.assert_not_called()


@pytest.mark.parametrize("reason", ["length", "content_filter", "tool_calls", None])
def test_local_real_finish_reason_is_not_discarded(monkeypatch, reason):
    import httpx

    monkeypatch.setattr("server.local.runtime.is_downloaded", lambda key: True)
    monkeypatch.setattr("server.local.serving.ensure_serving", lambda key: "http://mock.invalid")
    monkeypatch.setattr("server.local.serving.wire_model", lambda key: key)
    response = Mock()
    response.json.return_value = {
        "choices": [{"finish_reason": reason, "message": {"content": "partial"}}]
    }
    post = Mock(return_value=response)
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(RuntimeError, match="incomplete or refused"):
        oneshot.one_shot("sys", "usr", max_tokens=100, engine="local_test", source="compaction")
    assert post.call_count == 1
    assert post.call_args.kwargs["json"]["max_tokens"] == 100


@pytest.mark.parametrize("provider", ["claude", "gpt", "local_test"])
def test_complete_response_stays_on_selected_model(monkeypatch, provider):
    import httpx

    payload = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Complete summary"}]}
    bedrock = Mock()
    bedrock.invoke_model.return_value = {"body": io.BytesIO(json.dumps(payload).encode())}
    cloud = Mock()
    cloud.responses.create.return_value = SimpleNamespace(
        status="completed", output=[], output_text="Complete summary"
    )
    local = Mock()
    local.json.return_value = {
        "choices": [{"finish_reason": "stop", "message": {"content": "Complete summary"}}]
    }
    post = Mock(return_value=local)
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr("server.local.runtime.is_downloaded", lambda key: True)
    monkeypatch.setattr("server.local.serving.ensure_serving", lambda key: "http://mock.invalid")
    monkeypatch.setattr("server.local.serving.wire_model", lambda key: key)
    monkeypatch.setattr("server.openai_bedrock.runtime.build_sync_client", lambda region: cloud)
    monkeypatch.setattr("server.openai_bedrock.runtime.region_for", lambda key: "us-east-1")
    monkeypatch.setattr("server.costs.calls.record_responses", Mock())
    monkeypatch.setattr("server.costs.calls.record_invoke", Mock())
    out = oneshot.one_shot(
        "sys",
        "usr",
        max_tokens=100,
        engine=provider if provider == "local_test" else "cloud",
        cloud_model_key=provider,
        source="compaction",
        get_bedrock_client=lambda: bedrock,
    )
    assert out == "Complete summary"
    assert bedrock.invoke_model.call_count == int(provider == "claude")
    assert cloud.responses.create.call_count == int(provider == "gpt")
    assert post.call_count == int(provider == "local_test")


def test_serialization_preserves_opaque_tool_arguments():
    arguments = {"type": "base64", "data": "not-an-attachment", "url": "data:text/plain,verbatim"}
    messages = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t", "name": "n", "input": arguments}],
        }
    ]
    assert json.loads(compaction._serialize_history(messages)) == messages
    assert compaction._serialize_history(messages) == compaction._serialize_history(
        deepcopy(messages)
    )


def test_failed_summary_does_not_commit_successful_prepass(monkeypatch):
    messages = history()
    messages[0]["content"] = [{"type": "tool_result", "tool_use_id": "old", "content": "x" * 9000}]
    original = deepcopy(messages)
    assert compaction.estimate_message_size(
        compaction.microcompact_messages(messages)
    ) < compaction.estimate_message_size(messages)
    call = Mock(side_effect=RuntimeError("provider overflow"))
    monkeypatch.setattr(oneshot, "one_shot", call)
    with pytest.raises(compaction.CompactionError):
        compact(messages)
    assert messages == original
    assert call.call_count == 1


def test_microcompact_copies_nested_inputs_and_preserves_recent():
    messages = history()
    messages[0]["content"] = [{"type": "tool_result", "tool_use_id": "t", "content": "x" * 5000}]
    original = deepcopy(messages)
    out = compaction.microcompact_messages(messages)
    assert messages == original
    assert out[-6:] == messages[-6:]
    out[0]["content"][0]["content"] = "changed"
    assert messages == original
