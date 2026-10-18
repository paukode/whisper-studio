"""The stream guard on a turn that narrates its work and ends with a recap.

server/prompts/narration.py has the model say what it is about to do before
its tool calls and close with a recap whose table names each file by the link
its write result gave. In the tool_round_then_answer sequence (a round that
writes a file, then the answer), server/chat/claim_guard.py must show the
narration line, release the recap's table row with a ``deliveries`` receipt
for the file written, and release the sentence naming files only read
without one: driven on the guard alone, and through a whole chat turn
(tests/golden_harness.run_chat_turn), where the engine orders the frames.
"""

import asyncio
import json
import time

from server.chat.claim_guard import ClaimGuard
from server.chat.engine.events import Frame, RoundResult, TextDelta
from server.index.citations import created_file_link
from tests.golden_harness import (
    FakeBedrockClient,
    msg_end,
    msg_start,
    run_chat_turn,
    text_block,
    tool_use_block,
)

REL = "server/prompts/narration.py"
NARRATION = f"Writing the rule into {REL} so it stays in the cached block."
READ_ONLY = "I read server/goals/claims.py and src/utils/wsFileLinks.ts to check the shape."


async def _aiter(items):
    for item in items:
        yield item


def _round(guard: ClaimGuard, text: str, messages: list, calls: list[dict] | None = None):
    """One model round streamed 7 characters at a time, as the engine tests
    stream it, ending in a tool call when ``calls`` are given."""
    content = [{"type": "text", "text": text}, *(calls or [])]
    events: list = [TextDelta(text=text[i : i + 7]) for i in range(0, len(text), 7)]
    events.append(RoundResult(stop_reason="tool_use" if calls else "end_turn", content=content))

    async def go():
        return [ev async for ev in guard.wrap(_aiter(events), messages)]

    return asyncio.run(go()), content


def _text(items) -> str:
    return "".join(i.text for i in items if isinstance(i, TextDelta))


def _receipts(items) -> list[dict]:
    return [r for i in items if isinstance(i, Frame) for r in i.payload["deliveries"]["items"]]


def test_the_recap_row_gets_a_receipt_and_the_read_sentence_none(tmp_path):
    ws = tmp_path / "ws"
    full = ws / REL
    full.parent.mkdir(parents=True)
    guard = ClaimGuard(workspace=str(ws), started_at=time.time() - 5)
    messages: list = [{"role": "user", "content": "add the narration rule to the chat prompt"}]

    # Round 1: the narration line, then the write call.
    body = 'WORK_NARRATION = "..."\n'
    call = {
        "type": "tool_use",
        "id": "w1",
        "name": "ws_write_file",
        "input": {"path": REL, "content": body},
    }
    first, content = _round(guard, NARRATION, messages, [call])
    messages.append({"role": "assistant", "content": content})
    full.write_text(body)
    link = created_file_link(REL, str(full))
    result = (
        f"Wrote {REL} ({len(body)} bytes). "
        f"To reference this file for the user, copy this link verbatim: {link}"
    )
    messages.append(
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "w1", "content": result}],
        }
    )
    # The narration line claims nothing: it is shown before the call runs,
    # and nothing is left to release after it.
    assert _text(first) == NARRATION and _receipts(first) == []
    assert guard.after_tools(messages) == []

    # Round 2: the recap.
    row = f"| {link} | removed the legacy builder and added the section | +18 -6 |"
    recap = (
        "The narration rule is in the cloud chat prompt.\n\n"
        "- **Prompt**: one fixed section asks for the recap, so the cached block stays stable.\n\n"
        f"| File | What changed | Lines |\n|---|---|---|\n{row}\n\n"
        f"{READ_ONLY}\n\nNot verified: the live app."
    )
    second, _content = _round(guard, recap, messages)
    assert _text(second) == recap
    (receipt,) = _receipts(second)
    assert receipt["kind"] == "file" and receipt["target"] == str(full)
    # The receipt follows the row it verifies; the read sentence comes after
    # it with none of its own.
    (at,) = [i for i, item in enumerate(second) if isinstance(item, Frame)]
    assert _text(second[:at]).rstrip("\n").endswith(row)
    assert READ_ONLY in _text(second[at:])
    assert guard.finish() == []


def test_a_chat_turn_sends_the_receipt_right_after_the_recap_row(monkeypatch, tmp_path):
    # A document, so the turn's verification gate (code edited, no test run)
    # stays out of the way.
    rel = "docs/work-narration.md"
    ws = tmp_path / "ws"
    full = ws / rel
    full.parent.mkdir(parents=True)
    body = "# Showing your work\n"
    link = created_file_link(rel, str(full))

    async def write(name, given, **_kw):
        # The workspace tool's own result (server/approval/executors.py).
        assert name == "ws_write_file" and given["path"] == rel
        full.write_text(body)
        result = (
            f"Wrote {rel} ({len(body)} bytes). "
            f"To reference this file for the user, copy this link verbatim: {link}"
        )
        return result, []

    monkeypatch.setattr("server.chat.routes.get_workspace_path", lambda: str(ws))
    monkeypatch.setattr("server.tool_executor.route_tool", write)
    narration = f"Writing the rule into {rel} so the docs match."
    row = f"| {link} | new section | +1 -0 |"
    recap = (
        "The rule is documented.\n\n"
        f"| File | What changed | Lines |\n|---|---|---|\n{row}\n\n{READ_ONLY}"
    )
    client = FakeBedrockClient(
        [
            [
                msg_start(),
                *text_block(narration),
                *tool_use_block("tu_w", "ws_write_file", {"path": rel, "content": body}, index=1),
                *msg_end(stop_reason="tool_use"),
            ],
            [msg_start(), *text_block(recap), *msg_end()],
        ]
    )
    lines = run_chat_turn(monkeypatch, client, {"question": "document the narration rule"})
    frames = [json.loads(ln) for ln in lines if ln != "[DONE]"]
    shown = [(i, f["text"]) for i, f in enumerate(frames) if "text" in f]
    receipts = [(i, f["deliveries"]["items"]) for i, f in enumerate(frames) if "deliveries" in f]
    assert "".join(text for _i, text in shown) == narration + recap
    # One receipt, for the file written, and nothing before the recap row.
    ((at, (receipt,)),) = receipts
    assert receipt["kind"] == "file" and receipt["target"] == str(full)
    before = "".join(text for i, text in shown if i < at)
    assert before.rstrip("\n").endswith(row)
    assert before.startswith(narration)
    # The tool's result sits between the narration and the recap, and the
    # read sentence comes after the receipt with none of its own.
    tool_at = next(i for i, f in enumerate(frames) if "skill_result" in f)
    assert "".join(text for i, text in shown if i < tool_at) == narration
    assert "".join(text for i, text in shown if i > tool_at).startswith(recap[:20])
    assert READ_ONLY in "".join(text for i, text in shown if i > at)
