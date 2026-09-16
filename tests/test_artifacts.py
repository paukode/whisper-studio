"""server.artifacts: the apps the assistant delivered as cards can be read back
and edited in place instead of regenerated. Regression for the session where
"add English to it" rebuilt an 80 KB app from scratch, minutes of silence per
change, after the model searched its own past sessions for the HTML."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from server import artifacts as A


@pytest.fixture(autouse=True)
def _fresh():
    for sid in ("s1", "act-1", "act-2", "act-3", "route-1"):
        A.forget_session(sid)
    yield
    for sid in ("s1", "act-1", "act-2", "act-3", "route-1"):
        A.forget_session(sid)


def _html(n_lines: int = 10) -> str:
    body = "\n".join(f'<p id="p{i}">line {i}</p>' for i in range(n_lines))
    return (
        "<!DOCTYPE html>\n<html>\n<head><title>T</title></head>\n<body>\n"
        f"{body}\n<script>\nconst x = 1;\n</script>\n</body>\n</html>"
    )


def test_record_latest_and_title_match():
    A.record_artifact("s1", title="Todo App", html=_html(), description="d", tool_use_id="t1")
    A.record_artifact("s1", title="Weather", html=_html(3))
    assert A.latest_artifact("s1")["title"] == "Weather"
    assert A.latest_artifact("s1", "todo")["title"] == "Todo App"
    assert A.latest_artifact("s1", "nope") is None
    assert [a["title"] for a in A.list_artifacts("s1")] == ["Weather", "Todo App"]
    assert A.latest_artifact("unknown") is None


def test_history_fallback_reads_the_persisted_program_artifact():
    from server.infrastructure import sessions

    sessions._ensure_db()
    sessions._append_message_sync("s1", {"role": "user", "content": "make it"})
    sessions._append_message_sync(
        "s1",
        {
            "role": "assistant",
            "content": "done",
            "programArtifact": {"title": "Old App", "html": "<!DOCTYPE html><p>old</p>"},
            "timestamp": "t",
        },
    )
    art = A.latest_artifact("s1")
    assert art["title"] == "Old App" and art["source"] == "history"
    # A version recorded in this process outranks the persisted one.
    A.record_artifact("s1", title="Old App", html="<!DOCTYPE html><p>new</p>")
    assert A.latest_artifact("s1")["html"].endswith("new</p>")
    assert len(A.list_artifacts("s1")) == 2


def test_read_returns_whole_small_html_and_ranges_for_large():
    A.record_artifact("s1", title="Small", html=_html(5))
    out = json.loads(A.execute_read_artifact({}, "s1"))
    assert out["title"] == "Small" and out["html"].startswith("<!DOCTYPE")
    assert out["total_lines"] == _html(5).count("\n") + 1

    big = "<!DOCTYPE html>\n" + "\n".join(f'<div id="row{i}">{"x" * 80}</div>' for i in range(1000))
    A.record_artifact("s1", title="Big", html=big)
    out = json.loads(A.execute_read_artifact({}, "s1"))
    assert "html" not in out
    assert out["outline"] and out["lines"].startswith("1: <!DOCTYPE")
    rng = json.loads(A.execute_read_artifact({"start_line": 500, "end_line": 502}, "s1"))
    assert rng["start_line"] == 500 and rng["end_line"] == 502
    assert "row499" in rng["lines"] and "row502" not in rng["lines"]
    capped = json.loads(A.execute_read_artifact({"start_line": 1, "end_line": 900}, "s1"))
    assert capped["end_line"] == A.READ_RANGE_MAX_LINES
    past = json.loads(A.execute_read_artifact({"start_line": 5000}, "s1"))
    assert "error" in past
    assert "error" in json.loads(A.execute_read_artifact({"title": "zzz"}, "s1"))
    assert "error" in json.loads(A.execute_read_artifact({}, "empty-session"))


def test_edit_is_atomic_and_emits_the_updated_card():
    A.record_artifact("s1", title="App", html="<!DOCTYPE html><h1>Hello</h1><p>a</p><p>a</p>")
    out, fx = A.execute_edit_artifact(
        {
            "edits": [
                {"old_text": "<h1>Hello</h1>", "new_text": "<h1>Hi</h1>"},
                {"old_text": "<p>a</p>", "new_text": "<p>b</p>", "replace_all": True},
            ],
            "new_title": "App v2",
        },
        "s1",
        "tu9",
    )
    assert "updated" in out and "2 edit(s)" in out
    card = fx[0]["program_artifact"]
    assert card["html"] == "<!DOCTYPE html><h1>Hi</h1><p>b</p><p>b</p>"
    assert card["tool_use_id"] == "tu9" and card["title"] == "App v2"
    assert A.latest_artifact("s1")["title"] == "App v2"

    # Ambiguous without replace_all: refused, nothing changes.
    out, fx = A.execute_edit_artifact(
        {"edits": [{"old_text": "<p>b</p>", "new_text": "<p>c</p>"}]}, "s1"
    )
    assert fx == [] and "matches 2 places" in json.loads(out)["error"]
    assert A.latest_artifact("s1")["html"].count("<p>b</p>") == 2

    # A later failing edit rolls back the earlier one (atomic).
    out, fx = A.execute_edit_artifact(
        {
            "edits": [
                {"old_text": "<h1>Hi</h1>", "new_text": "<h1>X</h1>"},
                {"old_text": "nope", "new_text": ""},
            ]
        },
        "s1",
    )
    assert fx == [] and "not found" in json.loads(out)["error"]
    assert "<h1>Hi</h1>" in A.latest_artifact("s1")["html"]

    assert "error" in json.loads(A.execute_edit_artifact({"edits": []}, "s1")[0])
    assert "error" in json.loads(
        A.execute_edit_artifact({"edits": [{"old_text": "x", "new_text": "y"}]}, "no-art")[0]
    )


def test_activation_family_from_history_and_persisted_artifacts():
    from server.chat import tool_activation as TA

    TA.activate_from_history(
        "act-1",
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t", "name": "create_artifact", "input": {}}
                ],
            }
        ],
    )
    assert {"read_artifact", "edit_artifact"} <= set(TA.get_ordered("act-1"))
    # The persisted shape carries the artifact on the row, not the tool_use.
    TA.activate_from_history(
        "act-2",
        [
            {
                "role": "assistant",
                "content": "done",
                "programArtifact": {"title": "x", "html": "<p>"},
            }
        ],
    )
    assert {"create_artifact", "read_artifact", "edit_artifact"} <= set(TA.get_ordered("act-2"))
    TA.activate_from_history(
        "act-3",
        [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t", "name": "ws_grep", "input": {}}],
            }
        ],
    )
    assert "edit_artifact" not in TA.get_ordered("act-3")


def test_deliverable_check_accepts_an_edit_artifact_turn():
    from server.goals import deliverables as d

    msgs = [
        {"role": "user", "content": "add a title"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t", "name": "edit_artifact", "input": {}}],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]},
        {"role": "assistant", "content": "Updated the artifact card above."},
    ]
    assert d.artifact_created(msgs) is True
    assert d.check_claims(msgs, None) is None


def test_router_records_created_artifacts_and_dispatches_edits():
    from server.chat import tool_activation as TA
    from server.tool_router import route_tool

    executor = ThreadPoolExecutor(max_workers=1)

    async def _go():
        loop = asyncio.get_running_loop()
        common = dict(
            loop=loop,
            executor=executor,
            transcript="",
            attachments=None,
            session_id="route-1",
            model_id="",
        )
        out, fx = await route_tool(
            "create_artifact",
            {"title": "Clock", "html": "<!DOCTYPE html><h1>12:00</h1>", "description": "d"},
            tool_use_id="c1",
            **common,
        )
        assert "Clock" in out and fx[0]["program_artifact"]["title"] == "Clock"
        read_out, _ = await route_tool("read_artifact", {}, tool_use_id="r1", **common)
        read = json.loads(read_out)
        assert read["html"] == "<!DOCTYPE html><h1>12:00</h1>"
        out2, fx2 = await route_tool(
            "edit_artifact",
            {"edits": [{"old_text": "12:00", "new_text": "13:00"}]},
            tool_use_id="e1",
            **common,
        )
        assert "updated" in out2 and fx2[0]["program_artifact"]["html"].endswith("<h1>13:00</h1>")
        return out2

    try:
        asyncio.run(_go())
    finally:
        executor.shutdown(wait=False)
    assert {"read_artifact", "edit_artifact"} <= set(TA.get_ordered("route-1"))
    assert A.latest_artifact("route-1")["html"].endswith("<h1>13:00</h1>")


def test_tools_are_in_the_catalog():
    from server.chat.tool_pool import assemble_full_catalog

    names = {t["name"] for t in assemble_full_catalog()}
    assert {"create_artifact", "read_artifact", "edit_artifact"} <= names
