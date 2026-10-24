"""Thinking levels on local models (server/local/thinking.py).

The capability comes from the model's own chat template, read from a GGUF
file built here or an MLX snapshot folder. The levels map onto the request
fields llama-server b11851 and mlx-lm 0.32 read. No model is loaded.
"""

from __future__ import annotations

import json
import struct

import pytest

from server.infrastructure import effort
from server.local import thinking
from server.local.thinking import Caps


def _gguf_string(text: str) -> bytes:
    data = text.encode()
    return struct.pack("<Q", len(data)) + data


def _write_gguf(path, template: str | None, vocab_size: int = 3000) -> None:
    """A GGUF v3 header with the tokenizer arrays BEFORE the chat template, as
    the converter writes them."""
    kv = [
        _gguf_string("general.architecture") + struct.pack("<I", 8) + _gguf_string("qwen35"),
        _gguf_string("qwen35.block_count") + struct.pack("<I", 4) + struct.pack("<I", 32),
        _gguf_string("tokenizer.ggml.tokens")
        + struct.pack("<IIQ", 9, 8, vocab_size)
        + b"".join(_gguf_string(f"tok{i}") for i in range(vocab_size)),
        _gguf_string("tokenizer.ggml.scores")
        + struct.pack("<IIQ", 9, 6, vocab_size)
        + struct.pack(f"<{vocab_size}f", *([0.0] * vocab_size)),
    ]
    if template is not None:
        kv.append(
            _gguf_string("tokenizer.chat_template") + struct.pack("<I", 8) + _gguf_string(template)
        )
    header = b"GGUF" + struct.pack("<I", 3) + struct.pack("<QQ", 0, len(kv))
    path.write_bytes(header + b"".join(kv) + b"\0" * 64)


QWEN_LIKE = "{%- if enable_thinking is defined and enable_thinking is true %}<think>{% endif %}"
GPT_OSS_LIKE = "{{- 'Reasoning: ' + reasoning_effort }}<|channel|>analysis"
MINISTRAL_LIKE = "[THINK]{{ message['thinking'] }}[/THINK]"
PLAIN = "{% for m in messages %}<|im_start|>{{ m.role }}{{ m.content }}<|im_end|>{% endfor %}"


@pytest.mark.parametrize(
    "template, caps",
    [
        (QWEN_LIKE, Caps(True, False)),
        (GPT_OSS_LIKE, Caps(True, True)),
        (MINISTRAL_LIKE, Caps(True, False)),
        (PLAIN, Caps(False, False)),
        (None, Caps(False, False)),
    ],
)
def test_the_template_decides_whether_a_model_thinks(template, caps):
    assert thinking.caps_from_template(template) == caps


def test_the_chat_template_is_read_past_the_vocabulary(tmp_path):
    path = tmp_path / "m.gguf"
    _write_gguf(path, QWEN_LIKE)
    assert thinking.gguf_chat_template(str(path)) == QWEN_LIKE
    _write_gguf(path, None)
    assert thinking.gguf_chat_template(str(path)) is None
    path.write_bytes(b"not a gguf file at all" * 4)
    assert thinking.gguf_chat_template(str(path)) is None


def test_an_mlx_template_comes_from_the_files_mlx_lm_reads(tmp_path):
    (tmp_path / "tokenizer_config.json").write_text(json.dumps({"chat_template": QWEN_LIKE}))
    assert thinking.mlx_chat_template(str(tmp_path)) == QWEN_LIKE
    (tmp_path / "tokenizer_config.json").write_text(
        json.dumps({"chat_template": [{"name": "default", "template": GPT_OSS_LIKE}]})
    )
    assert GPT_OSS_LIKE in thinking.mlx_chat_template(str(tmp_path))
    (tmp_path / "chat_template.jinja").write_text(MINISTRAL_LIKE)
    assert thinking.mlx_chat_template(str(tmp_path)) == MINISTRAL_LIKE  # the .jinja file wins


@pytest.mark.parametrize(
    "caps, mlx, levels",
    [
        (Caps(True, False), False, ["none", "low", "medium", "high", "max"]),  # llama-server caps
        (Caps(True, True), True, ["none", "low", "medium", "high", "max"]),  # the template's own
        (Caps(True, False), True, ["none", "max"]),  # mlx-lm has no thinking cap
        (Caps(False, False), False, []),
    ],
)
def test_the_levels_follow_what_the_engine_can_enforce(monkeypatch, caps, mlx, levels):
    from server.local import runtime

    monkeypatch.setattr(thinking, "caps_for", lambda key: caps)
    monkeypatch.setattr(runtime, "is_mlx_model", lambda key: mlx)
    assert thinking.levels_for("local_x") == levels


def test_each_level_maps_to_the_llama_server_fields():
    caps = Caps(True, False)
    fields = {
        lv: thinking.request_fields(lv, caps, mlx=False, answer_tokens=4096)
        for lv in thinking.LEVELS
    }
    assert fields["none"] == {
        "max_tokens": 4096,
        "reasoning_effort": "none",
        "chat_template_kwargs": {"enable_thinking": False},
    }
    budgets = [fields[lv].get("thinking_budget_tokens") for lv in ("low", "medium", "high", "max")]
    assert budgets[:3] == sorted(budgets[:3]) and budgets[3] is None  # rising caps, max uncapped
    for lv in ("low", "medium", "high", "max"):
        assert fields[lv]["chat_template_kwargs"] == {"enable_thinking": True}
        # The thinking gets its own room on top of the answer cap.
        room = fields[lv]["max_tokens"] - 4096
        assert room >= (fields[lv].get("thinking_budget_tokens") or thinking.MAX_THINKING_TOKENS)


def test_a_template_with_its_own_levels_gets_the_level():
    llama = thinking.request_fields("max", Caps(True, True), mlx=False, answer_tokens=4096)
    assert llama["reasoning_effort"] == "high"  # its top level
    assert "thinking_budget_tokens" not in llama
    mlx = thinking.request_fields("low", Caps(True, True), mlx=True, answer_tokens=4096)
    assert mlx["chat_template_kwargs"] == {"enable_thinking": True, "reasoning_effort": "low"}


def test_mlx_turns_thinking_off_through_the_template_only():
    assert thinking.off_fields(mlx=True) == {"chat_template_kwargs": {"enable_thinking": False}}


def test_a_model_that_cannot_think_gets_no_thinking_fields():
    for level in (None, "max"):
        assert thinking.request_fields(
            level, thinking.NO_THINKING, mlx=False, answer_tokens=2048
        ) == {"max_tokens": 2048}


# ── the effort catalogue for local models ────────────────────────────────────


def test_a_local_model_offers_its_thinking_levels_and_starts_at_max(monkeypatch):
    monkeypatch.setattr(
        thinking, "levels_for", lambda key: ["none", "low", "medium", "high", "max"]
    )
    meta = {"is_local": True}
    assert effort.effort_levels_for(meta, "local_x") == ["none", "low", "medium", "high", "max"]
    assert effort.default_effort_for(meta, "local_x") == "max"
    assert (
        effort.resolve_effort(meta, "local_x", "extra") == "high"
    )  # a cloud-only rung clamps down
    monkeypatch.setattr(thinking, "levels_for", lambda key: [])
    assert effort.effort_levels_for(meta, "local_x") == []
    assert effort.resolve_effort(meta, "local_x", "max") is None  # no thinking, no fields


def test_a_thinking_level_never_clamps_to_off():
    assert effort.clamp_effort("high", ["none", "max"]) == "max"
    assert effort.clamp_effort("none", ["none", "max"]) == "none"
    # Cloud ladders keep their rule.
    assert effort.clamp_effort("extra", ["low", "medium", "high", "max"]) == "high"
    assert effort.clamp_effort("none", ["low", "medium", "high", "max"]) == "low"
