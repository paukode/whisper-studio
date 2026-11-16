"""An export holds each tool result in full.

The chat keeps a preview of a long tool result, and so does the saved session
row an export is made from. The whole text the model received is kept apart,
per session, and the export puts it back; deleting the session deletes it.
"""

import json
from types import SimpleNamespace

import pytest

from server import tool_executor
from server.infrastructure import sessions_routes, tool_results

SID = "sess-export"


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_results, "data_root", lambda: str(tmp_path))
    return tmp_path


def test_a_short_result_is_shown_whole_and_nothing_is_kept():
    state = SimpleNamespace(tool_id="t1")
    assert tool_executor._preview(state, "short", SID) == "short"
    assert tool_results.load(SID) == {}


def test_a_long_result_is_previewed_in_the_chat_and_kept_whole():
    long = "x" * (tool_executor.PREVIEW_CHARS + 500)
    state = SimpleNamespace(tool_id="t2")
    shown = tool_executor._preview(state, long, SID)
    assert shown == "x" * tool_executor.PREVIEW_CHARS + "..."
    assert tool_results.load(SID) == {"t2": long}


def test_the_export_puts_the_whole_result_back(monkeypatch):
    long = "y" * 5000
    tool_results.record(SID, "t3", long)
    history = [
        {"role": "user", "content": "run it"},
        {
            "role": "assistant",
            "content": "",
            "toolUse": [
                {"toolId": "t3", "toolName": "ws_run_command", "result": long[:2000] + "..."},
                {"toolId": "t4", "toolName": "ws_read_file", "result": "short"},
            ],
        },
    ]
    row = {
        "segments": "[]",
        "chat_history": json.dumps(history),
        "speaker_names": "{}",
        "title": "Export me",
    }
    _title, lines = sessions_routes._session_export_lines(SID, row)
    messages = [json.loads(line) for line in lines if json.loads(line)["type"] == "message"]
    cut, short = messages[1]["toolUse"]
    assert cut["result"] == long
    assert short["result"] == "short"
    # The saved row is not changed by an export.
    assert history[1]["toolUse"][0]["result"].endswith("...")


def test_the_last_text_wins_and_a_broken_line_is_skipped(data_dir):
    tool_results.record(SID, "t5", "first")
    with open(data_dir / "tool_results" / f"{SID}.jsonl", "a") as f:
        f.write('{"id": "t6", "text": "cut off\n')
    tool_results.record(SID, "t5", "second")
    assert tool_results.load(SID) == {"t5": "second"}


def test_deleting_the_session_deletes_its_kept_results(data_dir):
    tool_results.record(SID, "t7", "z" * 3000)
    tool_results.forget(SID)
    assert tool_results.load(SID) == {}
    tool_results.forget(SID)  # nothing left: no error


@pytest.mark.parametrize("bad", ["", None, "../x", "a/b", ".hidden", "_staging"])
def test_an_id_that_cannot_name_one_file_keeps_nothing(bad, data_dir):
    tool_results.record(bad, "t8", "text")
    assert tool_results.load(bad) == {}
    assert not (data_dir / "tool_results").exists()
