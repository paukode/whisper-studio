"""Nova Sonic event protocol: builders emit the documented shapes and the parser
labels model output by content block. Pure, no SDK."""

import base64
import json

from server.voice import protocol


def _ev(raw: str) -> dict:
    return json.loads(raw)["event"]


def test_session_start_clamps_endpointing():
    ev = _ev(protocol.session_start(endpointing="bogus"))
    assert ev["sessionStart"]["turnDetectionConfiguration"]["endpointingSensitivity"] == "MEDIUM"
    ev = _ev(protocol.session_start(endpointing="high"))
    assert ev["sessionStart"]["turnDetectionConfiguration"]["endpointingSensitivity"] == "HIGH"
    assert ev["sessionStart"]["inferenceConfiguration"]["maxTokens"] == 1024


def test_prompt_start_carries_voice_audio_and_tools():
    tools = [
        {
            "name": "ask_assistant",
            "description": "d",
            "input_schema": {"type": "object", "properties": {"request": {"type": "string"}}},
        }
    ]
    ev = _ev(protocol.prompt_start("p1", voice_id="matthew", tools=tools))["promptStart"]
    assert ev["promptName"] == "p1"
    audio = ev["audioOutputConfiguration"]
    assert audio["voiceId"] == "matthew"
    assert audio["sampleRateHertz"] == protocol.OUTPUT_SAMPLE_RATE
    assert audio["mediaType"] == "audio/lpcm" and audio["encoding"] == "base64"
    spec = ev["toolConfiguration"]["tools"][0]["toolSpec"]
    assert spec["name"] == "ask_assistant"
    # Sonic wants the JSON schema as a STRING.
    assert json.loads(spec["inputSchema"]["json"])["properties"]["request"]["type"] == "string"
    assert ev["toolConfiguration"]["toolChoice"] == {"auto": {}}
    assert ev["toolUseOutputConfiguration"] == {"mediaType": "application/json"}


def test_prompt_start_unknown_voice_falls_back_and_no_tools_omits_config():
    ev = _ev(protocol.prompt_start("p", voice_id="nobody"))["promptStart"]
    assert ev["audioOutputConfiguration"]["voiceId"] == protocol.DEFAULT_VOICE_ID
    assert "toolConfiguration" not in ev


def test_text_content_triplet_shares_names():
    start, body, end = (_ev(e) for e in protocol.text_content("p", "SYSTEM", "hi"))
    cs = start["contentStart"]
    assert cs["type"] == "TEXT" and cs["role"] == "SYSTEM" and cs["interactive"] is False
    assert body["textInput"]["content"] == "hi"
    assert body["textInput"]["contentName"] == cs["contentName"] == end["contentEnd"]["contentName"]
    typed = _ev(protocol.text_content("p", "USER", "x", interactive=True)[0])["contentStart"]
    assert typed["interactive"] is True and typed["role"] == "USER"


def test_audio_events_round_trip_pcm():
    pcm = bytes(range(64))
    start = _ev(protocol.audio_content_start("p", "a"))["contentStart"]
    cfg = start["audioInputConfiguration"]
    assert (
        cfg["sampleRateHertz"] == 16000 and cfg["sampleSizeBits"] == 16 and cfg["channelCount"] == 1
    )
    chunk = _ev(protocol.audio_input("p", "a", pcm))["audioInput"]
    assert base64.b64decode(chunk["content"]) == pcm
    assert chunk["contentName"] == "a"


def test_tool_result_triplet_references_tool_use_id():
    start, body, end = (_ev(e) for e in protocol.tool_result("p", "tu-1", {"ok": True}))
    cs = start["contentStart"]
    assert cs["type"] == "TOOL" and cs["role"] == "TOOL"
    assert cs["toolResultInputConfiguration"]["toolUseId"] == "tu-1"
    assert json.loads(body["toolResult"]["content"]) == {"ok": True}
    assert end["contentEnd"]["contentName"] == cs["contentName"]
    # Plain strings are wrapped: Sonic rejects non-object tool results.
    plain = _ev(protocol.tool_result("p", "tu-2", "done")[1])["toolResult"]["content"]
    assert json.loads(plain) == {"result": "done"}


def test_history_events_skip_other_roles_and_cap():
    history = [{"role": "cron_event", "content": "x"}, {"role": "user", "content": " "}]
    history += [{"role": "user" if i % 2 else "assistant", "content": f"m{i}"} for i in range(50)]
    events = protocol.history_events("p", history, limit=10)
    assert len(events) == 30  # 10 rows x 3 events
    roles = [_ev(e)["contentStart"]["role"] for e in events[::3]]
    assert set(roles) <= {"USER", "ASSISTANT"}
    assert _ev(events[-2])["textInput"]["content"] == "m49"


def _content_start(cid, role, typ, stage=None):
    body = {"contentId": cid, "role": role, "type": typ}
    if stage:
        body["additionalModelFields"] = json.dumps({"generationStage": stage})
    return json.dumps({"event": {"contentStart": body}})


def test_parser_labels_user_and_assistant_text():
    p = protocol.OutputParser()
    assert p.parse(_content_start("c1", "USER", "TEXT", "FINAL")) == []
    out = p.parse(json.dumps({"event": {"textOutput": {"contentId": "c1", "content": "hello"}}}))
    assert out == [{"kind": "user_transcript", "text": "hello"}]
    p.parse(_content_start("c2", "ASSISTANT", "TEXT", "SPECULATIVE"))
    out = p.parse(json.dumps({"event": {"textOutput": {"contentId": "c2", "content": "Sure."}}}))
    assert out[0]["kind"] == "assistant_text" and out[0]["stage"] == "speculative"
    end = p.parse(
        json.dumps({"event": {"contentEnd": {"contentId": "c2", "stopReason": "END_TURN"}}})
    )
    assert end == [
        {
            "kind": "assistant_text_end",
            "stage": "speculative",
            "stop_reason": "END_TURN",
            "content_id": "c2",
        }
    ]


def test_parser_decodes_audio_tool_use_interrupt_and_usage():
    p = protocol.OutputParser()
    pcm = b"\x01\x02\x03\x04"
    out = p.parse(
        json.dumps(
            {
                "event": {
                    "audioOutput": {
                        "contentId": "a",
                        "content": base64.b64encode(pcm).decode(),
                    }
                }
            }
        )
    )
    assert out == [{"kind": "audio", "pcm": pcm, "content_id": "a"}]
    out = p.parse(
        json.dumps(
            {
                "event": {
                    "toolUse": {
                        "toolUseId": "t1",
                        "toolName": "ask_assistant",
                        "content": json.dumps({"request": "run tests"}),
                    }
                }
            }
        )
    )
    assert out == [
        {
            "kind": "tool_use",
            "tool_use_id": "t1",
            "name": "ask_assistant",
            "input": {"request": "run tests"},
        }
    ]
    p.parse(_content_start("au", "ASSISTANT", "AUDIO"))
    out = p.parse(
        json.dumps({"event": {"contentEnd": {"contentId": "au", "stopReason": "INTERRUPTED"}}})
    )
    assert out == [{"kind": "interrupted"}]
    out = p.parse(
        json.dumps(
            {
                "event": {
                    "usageEvent": {
                        "totalTokens": 30,
                        "details": {
                            "total": {
                                "input": {"speechTokens": 10, "textTokens": 5},
                                "output": {"speechTokens": 12, "textTokens": 3},
                            }
                        },
                    }
                }
            }
        )
    )
    assert out[0]["kind"] == "usage" and out[0]["input_speech"] == 10
    assert out[0]["total_tokens"] == 30
    assert p.parse(json.dumps({"event": {"completionEnd": {"stopReason": "END_TURN"}}})) == [
        {"kind": "turn_end", "stop_reason": "END_TURN"}
    ]


def test_parser_tolerates_garbage():
    p = protocol.OutputParser()
    assert p.parse(b"\xff\xfe") == [{"kind": "error", "message": "unparseable event from model"}]
    assert p.parse("[]") == []
    assert p.parse(json.dumps({"event": {"somethingNew": {}}})) == []
