"""Claude Code transcripts (~/.claude/projects/<project>/<id>.jsonl) import as
new sessions through the same routes as Whisper's own exports.

A transcript is a parentUuid tree: rewinds leave abandoned branches, parallel
tool results hang off sibling branches, and /compact restarts the chain at a
boundary that links back through logicalParentUuid. These tests build small
transcripts in that shape and assert how the imported rows relate to them.
"""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.infrastructure import claude_code_import
from server.infrastructure.sessions import _ensure_db
from server.infrastructure.sessions import router as sessions_router


def _client():
    app = FastAPI()
    app.include_router(sessions_router)
    _ensure_db()
    return TestClient(app)


class _Transcript:
    """Appends entries that chain to the previous one unless told otherwise."""

    def __init__(self):
        self.lines: list[dict] = []
        self._n = 0
        self.last: str | None = None

    def add(self, kind: str, *, parent: str | None = "prev", **fields) -> str:
        self._n += 1
        uuid = f"u{self._n}"
        entry = {
            "type": kind,
            "uuid": uuid,
            "parentUuid": self.last if parent == "prev" else parent,
            "sessionId": "s1",
            "isSidechain": False,
            "timestamp": f"2026-09-20T10:00:{self._n:02d}.000Z",
            **fields,
        }
        self.lines.append(entry)
        self.last = uuid
        return uuid

    def user(self, content, **kw) -> str:
        return self.add("user", message={"role": "user", "content": content}, **kw)

    def assistant(self, *blocks, **kw) -> str:
        return self.add(
            "assistant",
            message={"role": "assistant", "model": "claude-opus-5-5", "content": list(blocks)},
            **kw,
        )

    def meta(self, kind: str, **fields) -> None:
        self.lines.append({"type": kind, "sessionId": "s1", **fields})

    def raw(self) -> str:
        return "\n".join(json.dumps(line) for line in self.lines) + "\n"


def _text(text):
    return {"type": "text", "text": text}


def _tool(tool_id, name="Bash", **inp):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inp}


def _result(tool_id, content, is_error=False):
    return {"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": is_error}


def test_detects_transcript_and_leaves_whisper_exports_alone():
    t = _Transcript()
    t.meta("mode", mode="normal")
    t.user("hello")
    assert claude_code_import.is_transcript(t.raw())
    export = json.dumps({"type": "export_meta", "title": "x"}) + "\n"
    assert not claude_code_import.is_transcript(export)


def test_prompts_replies_and_tool_calls_become_rows():
    t = _Transcript()
    t.meta("ai-title", aiTitle="Generated name")
    t.user("list the files")
    t.assistant(_text("Looking."))
    t.assistant(_tool("t1", command="ls"))
    t.user([_result("t1", "a.txt\nb.txt")])
    t.assistant(_tool("t2", command="cat missing"))
    t.user([_result("t2", "No such file", is_error=True)])
    t.assistant(_text("Two files."))

    title, rows = claude_code_import.parse(t.raw())

    assert title == "Generated name"
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[0]["content"] == "list the files"
    reply = rows[1]
    assert reply["content"] == "Looking.\n\nTwo files."
    assert [(u["toolId"], u["status"]) for u in reply["toolUse"]] == [
        ("t1", "complete"),
        ("t2", "error"),
    ]
    assert reply["toolUse"][0]["result"] == "a.txt\nb.txt"
    assert reply["toolUse"][0]["input"] == {"command": "ls"}
    # The reply is stamped when it starts, never before its prompt.
    assert rows[0]["timestamp"] <= reply["timestamp"]


def test_custom_title_beats_generated_and_first_prompt_is_the_last_resort():
    t = _Transcript()
    t.meta("ai-title", aiTitle="Generated")
    t.meta("custom-title", customTitle="Renamed")
    t.user("first prompt\nsecond line")
    t.assistant(_text("ok"))
    assert claude_code_import.parse(t.raw())[0] == "Renamed"

    bare = _Transcript()
    bare.user("first prompt\nsecond line")
    bare.assistant(_text("ok"))
    assert claude_code_import.parse(bare.raw())[0] == "first prompt"


def test_harness_traffic_is_dropped_and_prompts_are_unwrapped():
    t = _Transcript()
    t.user("<local-command-caveat>Caveat</local-command-caveat>", isMeta=True)
    t.user(
        "<command-name>/model</command-name>\n<command-message>model</command-message>\n<command-args></command-args>"
    )
    t.user("<local-command-stdout>Set model</local-command-stdout>")
    t.user("<system-reminder>context</system-reminder>\nreal question")
    t.assistant(_text("answer"))
    t.user("<task-notification>\n<task-id>x</task-id>\n</task-notification>")
    t.assistant(_text("the task finished"))
    t.user("Stop hook feedback: do more", isMeta=True)
    t.user("<command-name>/review</command-name>\n<command-args>the diff</command-args>")
    t.assistant(_text("reviewed"))
    t.add(
        "assistant",
        message={
            "role": "assistant",
            "model": "<synthetic>",
            "content": [_text("No response requested.")],
        },
    )

    _, rows = claude_code_import.parse(t.raw())

    # /model ran locally and got no reply, so it is not a turn; /review was answered.
    assert [(r["role"], r["content"]) for r in rows] == [
        ("user", "real question"),
        ("assistant", "answer\n\nthe task finished"),
        ("user", "/review the diff"),
        ("assistant", "reviewed"),
    ]


def test_api_errors_the_user_saw_are_kept():
    t = _Transcript()
    t.user("hi")
    t.add(
        "assistant",
        message={
            "role": "assistant",
            "model": "<synthetic>",
            "content": [_text("API Error: 529 Overloaded")],
        },
        isApiErrorMessage=True,
    )
    _, rows = claude_code_import.parse(t.raw())
    assert rows[-1] == {
        "role": "assistant",
        "content": "API Error: 529 Overloaded",
        "timestamp": rows[-1]["timestamp"],
    }


def test_rewound_branch_is_dropped_but_parallel_tool_results_are_kept():
    t = _Transcript()
    root = t.user("start")
    t.assistant(_text("first answer"))
    t.user("abandoned follow up")
    t.assistant(_text("abandoned answer"))
    # The user rewound to "start" and asked again: a sibling of the abandoned prompt.
    t.last = t.assistant(_text("first answer"), parent=root)
    t.user("kept follow up")
    a = t.assistant(_tool("p1", command="one"))
    b = t.assistant(_tool("p2", command="two"))
    # Parallel results: the first hangs off the first call, a sibling branch.
    t.user([_result("p1", "out one")], parent=a)
    t.last = b
    t.user([_result("p2", "out two")])
    t.assistant(_text("done"))

    _, rows = claude_code_import.parse(t.raw())

    contents = [r["content"] for r in rows]
    assert "abandoned follow up" not in contents and "abandoned answer" not in contents
    assert contents[-2:] == ["kept follow up", "done"]
    assert {u["toolId"]: u["result"] for u in rows[-1]["toolUse"]} == {
        "p1": "out one",
        "p2": "out two",
    }


def test_history_before_a_compaction_is_kept_and_the_summary_is_not():
    t = _Transcript()
    t.user("before compact")
    before = t.assistant(_text("early answer"))
    t.add("system", subtype="compact_boundary", parent=None, logicalParentUuid=before)
    t.user("This session is being continued from a previous conversation...", isCompactSummary=True)
    t.user("after compact")
    t.assistant(_text("late answer"))

    _, rows = claude_code_import.parse(t.raw())

    assert [r["content"] for r in rows] == [
        "before compact",
        "early answer",
        "after compact",
        "late answer",
    ]


def test_interrupt_and_queued_prompt_keep_their_place():
    t = _Transcript()
    t.user("long task")
    t.assistant(_text("working"))
    t.add(
        "attachment",
        attachment={"type": "queued_command", "prompt": "also do this", "commandMode": "prompt"},
    )
    t.assistant(_text("doing both"))
    t.user([_text("[Request interrupted by user]")])
    t.user("next")

    _, rows = claude_code_import.parse(t.raw())

    assert [(r["role"], r["content"]) for r in rows] == [
        ("user", "long task"),
        ("assistant", "working"),
        ("user", "also do this"),
        ("assistant", "doing both\n\n*(Stopped)*"),
        ("user", "next"),
    ]
    assert rows[3]["stopped"] is True


def test_long_tool_results_are_capped_like_a_live_turn():
    cap = claude_code_import.TOOL_RESULT_PREVIEW_CHARS
    t = _Transcript()
    t.user("dump it")
    t.assistant(_tool("big", name="Read", file_path="/x"))
    t.user([_result("big", [_text("x" * (cap * 3)), {"type": "image", "source": {}}])])
    _, rows = claude_code_import.parse(t.raw())
    result = rows[1]["toolUse"][0]["result"]
    assert result.startswith("x" * cap) and result.endswith("...")
    assert len(result) == cap + len("...")


def test_subagent_transcript_and_half_written_last_line():
    t = _Transcript()
    t.user("subagent brief", isSidechain=True)
    t.assistant(_text("subagent report"), isSidechain=True)
    raw = t.raw() + '{"type":"assistant","uuid":"cut'
    _, rows = claude_code_import.parse(raw)
    assert [r["content"] for r in rows] == ["subagent brief", "subagent report"]


def test_import_route_creates_a_session_from_a_transcript():
    client = _client()
    t = _Transcript()
    t.meta("ai-title", aiTitle="From Claude Code")
    t.user("question")
    t.assistant(_text("answer"))
    new_id = None
    try:
        r = client.post(
            "/api/sessions/import-transcript",
            files={"file": ("380a4e15.jsonl", t.raw(), "application/x-ndjson")},
        )
        assert r.status_code == 200, r.text
        new_id = r.json()["new_session_id"]
        assert r.json()["title"] == "From Claude Code"
        imported = client.get(f"/api/sessions/{new_id}").json()
        assert [(m["role"], m["content"]) for m in imported["chatHistory"]] == [
            ("user", "question"),
            ("assistant", "answer"),
        ]
        assert imported["segments"] == []
    finally:
        if new_id:
            client.delete(f"/api/sessions/{new_id}")


def test_bulk_import_takes_transcripts_and_names_an_empty_one():
    client = _client()
    good = _Transcript()
    good.user("question")
    good.assistant(_text("answer"))
    stub = _Transcript()
    stub.meta("ai-title", aiTitle="never started")
    r = client.post(
        "/api/sessions/bulk-import",
        files=[
            ("files", ("good.jsonl", good.raw(), "application/x-ndjson")),
            ("files", ("stub.jsonl", stub.raw(), "application/x-ndjson")),
        ],
    )
    try:
        assert r.status_code == 200, r.text
        body = r.json()
        assert [i["filename"] for i in body["imported"]] == ["good.jsonl"]
        assert body["failed"] == [
            {"filename": "stub.jsonl", "error": "this Claude Code transcript has no messages"}
        ]
    finally:
        for item in r.json().get("imported", []):
            client.delete(f"/api/sessions/{item['new_session_id']}")
