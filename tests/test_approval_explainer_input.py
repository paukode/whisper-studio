"""The approval card's risk explainer: what it reads and how its text is cut.

Field report: cards in Bypass warned about a Keychain read no call asked for
(the gate stamps ``secret_access`` on every Bypass call and the raw payload
went to the explainer), and the card's text stopped mid-word.
"""

import asyncio
import json
from unittest.mock import AsyncMock

from server.approval.bootstrap import register_defaults
from server.approval.markers import approval_request
from server.security import explainer
from server.security.permissions import MODE_BYPASS, MODE_DEFAULT
from server.tool_executor import process_tool_results

register_defaults()


class _State:
    def __init__(self, tool_id, tool_name, output):
        self.tool_id = tool_id
        self.tool_name = tool_name
        self.output = output
        self.tool_use = {"id": tool_id, "name": tool_name, "input": {}}
        self.status = "done"
        self.side_effects = []


def _explained_input(monkeypatch, payload: dict, mode: str) -> dict:
    monkeypatch.setattr(
        "server.security.permissions.load_permissions",
        lambda: {"mode": mode, "rules": []},
    )
    explain = AsyncMock(return_value=None)
    monkeypatch.setattr("server.tool_approvals.explain_permission", explain)
    state = _State("tu_1", payload["action"], approval_request(json.dumps(payload)))
    asyncio.run(
        process_tool_results(
            [state],
            budget_fn=lambda _n, out: out,
            session_approvals={},
            config={"permission_explainer_enabled": True},
            model_id="test-model",
            recent_messages=[],
            mode=mode,
        )
    )
    assert explain.await_count == 1, "the card was expected to ask"
    return explain.call_args.kwargs["tool_input"]


def test_a_bypass_card_sends_no_server_stamps_to_the_explainer(monkeypatch):
    # A GitHub repo delete still asks in Bypass; the gate stamps
    # secret_access=true on its payload all the same.
    sent = _explained_input(
        monkeypatch,
        {
            "action": "github_destructive",
            "args": ["repo", "delete", "demo/sample", "--yes"],
            "session_id": "s1",
            "__secret_reads__": True,
        },
        MODE_BYPASS,
    )
    assert sent.get("args") == ["repo", "delete", "demo/sample", "--yes"]
    assert "secret_access" not in sent
    assert not any(key.startswith("__") for key in sent)
    assert "session_id" not in sent


def test_a_card_that_asks_for_a_keychain_read_tells_the_explainer(monkeypatch):
    sent = _explained_input(
        monkeypatch,
        {"action": "terminal_run", "command": "security find-identity", "secret_access": True},
        MODE_DEFAULT,
    )
    assert sent.get("secret_access") is True
    assert sent.get("command") == "security find-identity"


def test_clip_words_never_cuts_inside_a_word():
    text = "The command deletes every generated report under the build folder tonight"
    for limit in range(20, len(text)):
        clipped = explainer.clip_words(text, limit)
        assert len(clipped) <= limit
        assert clipped.endswith("…")
        body = clipped[:-1]
        assert text.startswith(body)
        # The next character in the original is a space: the cut fell
        # between two words.
        assert text[len(body)] == " ", (limit, clipped)
    assert explainer.clip_words(text, len(text)) == text


def test_the_explainer_input_is_valid_json_with_whole_words():
    long_command = " ".join(["echo word"] * 200)
    message = explainer._build_user_message(
        "terminal_run", {"command": long_command, "mode": "sandbox"}, []
    )
    line = next(ln for ln in message.splitlines() if ln.startswith("Input: "))
    data = json.loads(line[len("Input: ") :])
    assert data["mode"] == "sandbox"
    assert data["command"].endswith("…")
    assert long_command.startswith(data["command"][:-1])
    assert long_command[len(data["command"]) - 1] == " "


def test_an_input_with_many_fields_drops_whole_fields():
    fields = {f"field{i}": "x" * 300 for i in range(20)}
    message = explainer._build_user_message("some_tool", fields, [])
    line = next(ln for ln in message.splitlines() if ln.startswith("Input: "))
    data = json.loads(line[len("Input: ") :])
    assert "..." in data
    assert all(data[k] == fields[k] for k in data if k != "...")
