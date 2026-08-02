"""OpenAI-on-Bedrock runtime: offline tests for the pure request-mapping
helpers and region/provider resolution (no AWS / network needed). The
streaming adapter itself is covered in tests/test_engine_openai.py."""

import asyncio
import types

from server.openai_bedrock import runtime as oai


class FakeEvent:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeStream:
    """Async-iterable stand-in for the openai responses stream."""

    def __init__(self, events):
        self._events = events
        self.closed = False

    def __aiter__(self):
        async def gen():
            for e in self._events:
                yield e

        return gen()

    async def close(self):
        self.closed = True


async def _collect(agen):
    out = []
    async for x in agen:
        out.append(x)
    return out


# ── pure mapping helpers ────────────────────────────────────────────────────


def test_reasoning_effort_mapping(monkeypatch):
    # Decouple from the live config catalog: fake metas for one model whose
    # ladder tops at xhigh (5.5) and one whose ladder includes max (5.6).
    ids = {"g55": "openai.gpt-5.5", "g56": "openai.gpt-5.6-sol"}
    monkeypatch.setattr(oai, "_model_meta", lambda k: {"id": ids.get(k, "")})
    assert oai.reasoning_effort_for("g55", "none") == "none"
    assert oai.reasoning_effort_for("g55", "low") == "low"
    assert oai.reasoning_effort_for("g55", "medium") == "medium"
    assert oai.reasoning_effort_for("g55", "high") == "high"
    assert oai.reasoning_effort_for("g55", "extra") == "xhigh"
    assert oai.reasoning_effort_for("g55", "max") == "xhigh"
    assert oai.reasoning_effort_for("g55", "ultracode") == "xhigh"
    assert oai.reasoning_effort_for("g55", None) == "medium"
    assert oai.reasoning_effort_for("g55", "bogus") == "medium"
    # GPT-5.6 sends the real "max" tier for the app's top labels only.
    assert oai.reasoning_effort_for("g56", "max") == "max"
    assert oai.reasoning_effort_for("g56", "ultracode") == "max"
    assert oai.reasoning_effort_for("g56", "high") == "high"
    assert oai.reasoning_effort_for("g56", "extra") == "xhigh"
    assert oai.reasoning_effort_for("g56", None) == "medium"
    # Unknown model meta degrades to the xhigh-capped map.
    assert oai.reasoning_effort_for("nope", "max") == "xhigh"


def test_translate_tools_flat_function_shape():
    pool = [
        {
            "name": "read_file",
            "description": "Read a file",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]
    out = oai.translate_tools(pool)
    assert out == [
        {
            "type": "function",
            "name": "read_file",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]
    # Missing schema gets a safe empty-object default (Responses requires one).
    assert oai.translate_tools([{"name": "x"}])[0]["parameters"] == {
        "type": "object",
        "properties": {},
    }


def test_to_responses_input_roles_and_passthrough():
    msgs = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}, {"type": "text", "text": "there"}],
        },
        {"role": "tool", "content": "x"},  # unknown role -> coerced to user
    ]
    items = oai.to_responses_input(msgs)
    assert items[0] == {"role": "user", "content": "hello"}  # string passes through
    # list content -> typed parts (output_text for assistant)
    assert items[1] == {
        "role": "assistant",
        "content": [
            {"type": "output_text", "text": "hi"},
            {"type": "output_text", "text": "there"},
        ],
    }
    assert items[2]["role"] == "user" and items[2]["content"] == "x"


def test_multimodal_content_maps_text_and_images():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this?"},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
                },
            ],
        }
    ]
    parts = oai.to_responses_input(msgs)[0]["content"]
    assert {"type": "input_text", "text": "what is this?"} in parts
    assert any(
        p["type"] == "input_image" and p["image_url"] == "data:image/png;base64,AAAA" for p in parts
    )
    # Assistant list-content stays output_text; plain strings pass through.
    assert oai.to_responses_input(
        [{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]
    )[0]["content"] == [{"type": "output_text", "text": "hi"}]
    assert oai.to_responses_input([{"role": "user", "content": "hello"}])[0]["content"] == "hello"


def test_tool_result_input_items_text():
    # Plain-text result -> a single function_call_output (no extra message).
    items = oai.tool_result_input_items("call_1", "done: 3 files")
    assert items == [
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "done: 3 files",
        }
    ]
    # Non-str content is coerced (never crashes on odd shapes).
    assert oai.tool_result_input_items("c", 42)[0]["output"] == "42"


def test_tool_result_input_items_image_becomes_input_image():
    # A screenshot result (image block + caption) must reach GPT-5.x as a real
    # image, not a str()-ed base64 blob: caption -> function_call_output, image
    # -> a follow-up user message with an input_image part.
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "ZZZZ"}},
        {"type": "text", "text": "Screenshot of /login"},
    ]
    items = oai.tool_result_input_items("call_shot", content)
    assert len(items) == 2
    assert items[0] == {
        "type": "function_call_output",
        "call_id": "call_shot",
        "output": "Screenshot of /login",
    }
    assert items[1]["role"] == "user"
    assert {"type": "input_image", "image_url": "data:image/jpeg;base64,ZZZZ"} in items[1][
        "content"
    ]
    # Nothing in the output should carry the raw base64 payload.
    assert "ZZZZ" not in items[0]["output"]


def test_tool_result_input_items_image_without_caption_gets_pointer():
    # Responses rejects an empty function_call_output; when the tool gave no
    # text, leave a pointer so the pairing stays valid.
    items = oai.tool_result_input_items(
        "c",
        [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
            },
        ],
    )
    assert items[0]["output"]  # non-empty
    assert items[1]["role"] == "user"


def test_is_openai_model(monkeypatch):
    monkeypatch.setattr(
        oai, "_model_meta", lambda k: {"provider": "openai_bedrock"} if k == "gpt5.5" else {}
    )
    assert oai.is_openai_model("gpt5.5") is True
    assert oai.is_openai_model("opus") is False


def test_region_for_uses_bedrock_region_when_no_override(monkeypatch):
    # No per-model override => same account-wide bedrock_region the Anthropic
    # path uses. Whatever config says is what we get — no us-east-2 fallback.
    monkeypatch.setattr(oai, "_model_meta", lambda k: {"provider": "openai_bedrock"})
    monkeypatch.setattr(oai, "load_config", lambda: {"bedrock_region": "eu-west-1"})
    assert oai.region_for("gpt5.5") == "eu-west-1"


def test_region_for_openai_region_override_wins(monkeypatch):
    monkeypatch.setattr(oai, "_model_meta", lambda k: {"openai_region": "us-west-2"})
    monkeypatch.setattr(oai, "load_config", lambda: {"bedrock_region": "us-east-1"})
    assert oai.region_for("gpt5.4") == "us-west-2"


def test_region_for_blank_override_falls_back_to_bedrock_region(monkeypatch):
    # A whitespace-only override must not produce region="" — fall back cleanly.
    monkeypatch.setattr(oai, "_model_meta", lambda k: {"openai_region": "   "})
    monkeypatch.setattr(oai, "load_config", lambda: {"bedrock_region": "us-east-1"})
    assert oai.region_for("gpt5.5") == "us-east-1"


def test_region_for_has_no_hardcoded_default(monkeypatch):
    # An arbitrary configured region flows straight through — proves there is no
    # baked-in us-east-2 masking the config value.
    monkeypatch.setattr(oai, "_model_meta", lambda k: {})
    monkeypatch.setattr(oai, "load_config", lambda: {"bedrock_region": "ap-southeast-2"})
    assert oai.region_for("gpt5.5") == "ap-southeast-2"


# --- Config propagation: a GPT model is as turnkey as a Claude one ---


def test_provider_inferred_from_openai_id():
    # Minimal entry (id + label only) — provider, effort tier, and verbosity are
    # all inferred, and no region is pinned so it follows bedrock_region.
    from server.infrastructure.config import _normalize_chat_models

    _ids, meta = _normalize_chat_models(
        {"gpt5.6": {"id": "openai.gpt-5.6-sol", "label": "GPT-5.6"}}
    )
    m = meta["gpt5.6"]
    # provider is what is_openai_model() keys off — inference alone makes it route.
    assert m["provider"] == "openai_bedrock"
    assert m["effort_tier"] == "openai"
    assert m["verbosity"] == "medium"
    assert m["openai_region"] is None


def test_explicit_provider_overrides_id_inference():
    from server.infrastructure.config import _normalize_chat_models

    _ids, meta = _normalize_chat_models({"weird": {"id": "openai.custom", "provider": "anthropic"}})
    assert meta["weird"]["provider"] == "anthropic"
    # Non-OpenAI provider => effort tier inferred from the key, not the openai ladder.
    assert meta["weird"]["effort_tier"] != "openai"


def test_anthropic_id_stays_anthropic():
    from server.infrastructure.config import _normalize_chat_models

    _ids, meta = _normalize_chat_models(
        {"opus4.8": {"id": "global.anthropic.claude-opus-4-8", "label": "Opus 4.8"}}
    )
    assert meta["opus4.8"]["provider"] == "anthropic"


# --- Transcript threading through tool_ctx (PR #135 follow-up) ---
