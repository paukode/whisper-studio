"""Approval cards are the server's: a Yes runs what the server showed, once.

POST /api/approval/execute names the card by its id (server/approval/cards.py).
It used to take the action and payload the client posted back, with nothing
recording which cards were shown or answered: a retry or a second click ran
the action twice, and a payload no card ever showed could run.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.approval import cards, registry
from server.approval.router import router as approval_router
from server.approval.spec import ApprovalOutcome, ApprovalSpec

ran: list[dict] = []


async def _record(payload: dict) -> ApprovalOutcome:
    ran.append(dict(payload))
    return ApprovalOutcome(ok=True, output="done")


@pytest.fixture
def client():
    ran.clear()
    registry.register(
        "test_card_action",
        ApprovalSpec(category="test-card", preview="text", summary="t", executor=_record),
    )
    registry.register(
        "test_direct_action",
        ApprovalSpec(
            category="test-direct", preview="text", summary="t", executor=_record, direct=True
        ),
    )
    app = FastAPI()
    app.include_router(approval_router)
    with TestClient(app) as c:
        yield c


def _card(payload: dict | None = None, *, session_id: str = "s1", tool_use_id: str = "tu_1") -> str:
    return cards.issue(
        session_id=session_id,
        action="test_card_action",
        payload=payload if payload is not None else {"path": "a.py"},
        tool_use_id=tool_use_id,
    )


def test_a_card_runs_once(client):
    card_id = _card()

    first = client.post("/api/approval/execute", json={"card_id": card_id}).json()
    again = client.post("/api/approval/execute", json={"card_id": card_id}).json()

    assert first["ok"] is True
    assert again == {"ok": False, "error": cards.UNKNOWN}
    assert len(ran) == 1


def test_the_card_runs_what_the_server_showed_not_what_is_posted(client):
    card_id = _card({"path": "a.py"})

    client.post(
        "/api/approval/execute",
        json={"card_id": card_id, "action": "test_card_action", "payload": {"path": "/etc/x"}},
    )

    assert ran[0]["path"] == "a.py"
    assert ran[0]["__tool_use_id__"] == "tu_1"


def test_an_action_posted_without_a_card_does_not_run(client):
    res = client.post(
        "/api/approval/execute", json={"action": "test_card_action", "payload": {"path": "a.py"}}
    )

    assert res.status_code == 403
    assert ran == []


def test_a_direct_action_runs_without_a_card(client):
    # Answering an MCP elicitation in Settings has no card behind it.
    res = client.post(
        "/api/approval/execute", json={"action": "test_direct_action", "payload": {"x": 1}}
    )

    assert res.json()["ok"] is True
    assert ran == [{"x": 1}]


def test_a_card_answered_by_the_turn_cannot_run_later(client):
    # The user said No: the continuation carries the denial, and a Yes posted
    # afterwards (a stale tab, a retry) must not run the action.
    card_id = _card(session_id="s2", tool_use_id="tu_no")
    cards.close_answered("s2", ["tu_no"])

    res = client.post("/api/approval/execute", json={"card_id": card_id}).json()

    assert res["ok"] is False
    assert ran == []


def test_a_card_the_server_never_showed_is_refused_with_a_reason(client):
    res = client.post("/api/approval/execute", json={"card_id": "nope"}).json()

    assert res == {"ok": False, "error": cards.UNKNOWN}


def test_a_running_card_is_not_taken_twice():
    card_id = _card()

    assert isinstance(cards.take(card_id), cards.Card)
    assert cards.take(card_id) == cards.TAKEN
    # The turn's answer arriving while it runs leaves the running record alone.
    cards.close_answered("s1", ["tu_1"])
    assert cards.take(card_id) == cards.TAKEN
    cards.finish(card_id)
    assert cards.take(card_id) == cards.UNKNOWN


def test_reset_drops_only_that_sessions_pending_cards():
    mine = _card(session_id="s-reset")
    other = _card(session_id="s-other")

    assert cards.forget_session("s-reset") == 1

    assert cards.take(mine) == cards.UNKNOWN
    assert isinstance(cards.take(other), cards.Card)
    cards.finish(other)
