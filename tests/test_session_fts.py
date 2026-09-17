"""server.infrastructure.session_search (FTS5 mirror) + the session_search tool: the FTS5
mirror stays in step with the sessions store, and the four tool shapes return
real stored messages."""

from __future__ import annotations

import asyncio
import json

import pytest

from server.agent_tools.session_search_tool import execute_session_search
from server.infrastructure import session_search as ss
from server.infrastructure import sessions


@pytest.fixture(autouse=True)
def _flag(monkeypatch):
    monkeypatch.setattr("server.agent_tools.session_search_tool._enabled", lambda: True)


def _seed():
    sessions._ensure_db()
    sessions._append_message_sync(
        "kube", {"role": "user", "content": "How do I roll back a kubernetes deployment?"}
    )
    sessions._append_message_sync(
        "kube",
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "Use kubectl rollout undo on the deployment."}],
        },
    )
    sessions._append_message_sync(
        "kube", {"role": "user", "content": "Thanks, and what about helm charts?"}
    )
    sessions._append_message_sync(
        "py", {"role": "user", "content": "Explain python dataclasses please"}
    )
    sessions._append_message_sync(
        "py", {"role": "assistant", "content": "A dataclass generates init and repr."}
    )
    with sessions._get_conn() as conn:
        conn.execute("UPDATE sessions SET title = 'K8s rollback' WHERE id = 'kube'")
        conn.execute("UPDATE sessions SET title = 'Dataclasses' WHERE id = 'py'")


def test_fts_is_available_and_mirrors_appends():
    _seed()
    assert ss.available() is True
    hits = ss.search("kubernetes deployment")
    assert [h["session_id"] for h in hits] == ["kube"]
    assert hits[0]["title"] == "K8s rollback" and hits[0]["msg_index"] == 0
    assert "kubernetes" in hits[0]["snippet"].lower()


def test_stemming_prefix_and_fts_syntax():
    _seed()
    # porter stemming: "deployments" finds "deployment"
    assert [h["session_id"] for h in ss.search("deployments")] == ["kube"]
    # prefix mode (sidebar as-you-type)
    assert [h["session_id"] for h in ss.search("datacl", prefix=True)] == ["py"]
    # raw FTS5 syntax passes through
    assert {h["session_id"] for h in ss.search("kubernetes OR dataclasses")} == {"kube", "py"}
    # unbalanced quotes do not raise, they fall back to the tolerant form
    assert ss.search('"helm') == [] or ss.search('"helm')[0]["session_id"] == "kube"


def test_exclude_and_restrict_to_a_session():
    _seed()
    assert ss.search("please", exclude_session="py") == []
    assert [h["session_id"] for h in ss.search("helm", session_id="kube")] == ["kube"]
    assert ss.search("helm", session_id="py") == []


def test_read_window_scroll_and_browse():
    _seed()
    whole = ss.read_window("kube")
    assert whole["message_count"] == 3 and [m["index"] for m in whole["messages"]] == [0, 1, 2]
    win = ss.read_window("kube", around_index=2, window=1)
    assert [m["index"] for m in win["messages"]] == [1, 2]
    assert win["messages_before"] == 1 and win["messages_after"] == 0
    assert ss.read_window("nope") is None
    recent = ss.browse(limit=5)
    assert {s["session_id"] for s in recent} == {"kube", "py"}
    assert all(s["preview"] for s in recent)


def test_upsert_reindexes_and_delete_drops_rows():
    _seed()
    sessions._upsert_session(
        "kube",
        title="K8s rollback",
        custom_title=0,
        generated_title=0,
        created_at="a",
        updated_at="b",
        segments="[]",
        chat_history_frontend=[{"role": "user", "content": "now about terraform state"}],
        speaker_names="{}",
        workspace_path="",
        compaction_count=0,
        latched_config="{}",
    )
    assert [h["session_id"] for h in ss.search("terraform")] == ["kube"]
    assert [h["session_id"] for h in ss.search("rollback")] == ["kube"]  # title row
    assert ss.search("kubernetes") == []  # the old text is gone from the mirror
    sessions._delete_session_sync("kube")
    assert ss.search("terraform") == []


def test_tool_shapes():
    _seed()
    browse = json.loads(execute_session_search({}, "kube"))
    assert browse["shape"] == "browse" and len(browse["sessions"]) == 2

    disc = json.loads(execute_session_search({"query": "kubernetes"}, "py"))
    assert disc["shape"] == "discovery" and disc["count"] == 1
    top = disc["results"][0]
    assert top["session_id"] == "kube" and top["is_current_session"] is False
    assert top["messages"] and top["messages"][0]["index"] == 0

    read = json.loads(execute_session_search({"session_id": "kube"}, "py"))
    assert read["shape"] == "read" and read["message_count"] == 3

    scroll = json.loads(
        execute_session_search({"session_id": "kube", "around_index": 1, "window": 1}, "py")
    )
    assert scroll["shape"] == "scroll" and [m["index"] for m in scroll["messages"]] == [0, 1, 2]

    assert "error" in json.loads(execute_session_search({"session_id": "missing"}, "py"))


def test_sidebar_endpoint_uses_the_index(monkeypatch):
    _seed()
    from server.infrastructure.sessions_routes import search_sessions

    out = asyncio.run(search_sessions(q="kuber", limit=10))
    assert [r["id"] for r in out["results"]] == ["kube"] and out["truncated"] is False
    assert asyncio.run(search_sessions(q=""))["results"] == []


def test_reindex_is_incremental_and_edits_reindex_from_the_change():
    """A save rewrites only the rows from the first changed message on: an
    append leaves earlier rows (their rowids) untouched, an edit to message 0
    replaces its text in the mirror and keeps the later messages searchable."""
    _seed()

    def rows():
        with sessions._get_conn() as conn:
            return {
                int(r[0]): int(r[1])
                for r in conn.execute(
                    f"SELECT msg_index, rowid FROM {ss.FTS_TABLE} WHERE session_id = 'kube'"
                )
            }

    before = rows()
    sessions._append_message_sync(
        "kube", {"role": "assistant", "content": "Helm has its own rollback command."}
    )
    after = rows()
    assert all(after[i] == rid for i, rid in before.items())
    assert 3 in after
    assert [h["session_id"] for h in ss.search("rollback command")] == ["kube"]

    with sessions._get_conn() as conn:
        history = json.loads(
            conn.execute("SELECT chat_history FROM sessions WHERE id = 'kube'").fetchone()[0]
        )
    history[0] = {"role": "user", "content": "How do I undo a deployment on openshift?"}
    sessions._upsert_session(
        "kube",
        title="K8s rollback",
        custom_title=0,
        generated_title=0,
        created_at="a",
        updated_at="b",
        segments="[]",
        chat_history_frontend=history,
        speaker_names="{}",
        workspace_path="",
        compaction_count=0,
        latched_config="{}",
    )
    assert [h["session_id"] for h in ss.search("openshift")] == ["kube"]
    assert ss.search("kubernetes") == []
    assert [h["session_id"] for h in ss.search("helm")] == ["kube"]
    # Every row still sits inside the session's own rowid range.
    with sessions._get_conn() as conn:
        seq = conn.execute(
            f"SELECT seq FROM {ss.STATE_TABLE} WHERE session_id = 'kube'"
        ).fetchone()[0]
    assert all(seq * ss._SPAN <= rid < (seq + 1) * ss._SPAN for rid in rows().values())
