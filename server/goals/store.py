"""Goal + goal_state persistence on the sessions row.

A goal is 1:1 with a session, so it lives on two additive columns (migration
009 in sessions._MIGRATED_SESSION_COLUMNS) rather than a new table. goal_state
is a small JSON blob holding the live counters and last verdict:

    {"active": bool, "consecutive_blocks": int, "total_evals": int,
     "last_verdict": str, "last_feedback": str, "set_at": iso8601}
"""

from __future__ import annotations

import json

from server.infrastructure.sessions import _get_conn

_EMPTY_STATE = {
    "active": False,
    "consecutive_blocks": 0,
    "total_evals": 0,
    "last_verdict": "",
    "last_feedback": "",
    "set_at": "",
}


def _load_state(session_id: str) -> dict:
    with _get_conn() as conn:
        row = conn.execute("SELECT goal_state FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row or not row["goal_state"]:
        return dict(_EMPTY_STATE)
    try:
        data = json.loads(row["goal_state"])
    except (ValueError, TypeError):
        return dict(_EMPTY_STATE)
    return {**_EMPTY_STATE, **(data if isinstance(data, dict) else {})}


def get_goal(session_id: str) -> dict:
    """Return ``{"goal": str, "state": {...}}`` for a session (empty if none)."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT goal, goal_state FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    if not row:
        return {"goal": "", "state": dict(_EMPTY_STATE)}
    state = dict(_EMPTY_STATE)
    if row["goal_state"]:
        try:
            state = {**_EMPTY_STATE, **json.loads(row["goal_state"])}
        except (ValueError, TypeError):
            pass
    return {"goal": row["goal"] or "", "state": state}


def set_goal(session_id: str, goal: str, *, set_at: str = "") -> dict:
    """Set (or replace) a session's goal and reset its state to a fresh active
    goal. ``set_at`` is an ISO timestamp supplied by the caller (the store never
    reads the clock, keeping it deterministic for tests)."""
    goal = (goal or "").strip()
    state = dict(_EMPTY_STATE)
    if goal:
        state.update(active=True, set_at=set_at)
    with _get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET goal = ?, goal_state = ? WHERE id = ?",
            (goal, json.dumps(state), session_id),
        )
    return {"goal": goal, "state": state}


def clear_goal(session_id: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET goal = '', goal_state = ? WHERE id = ?",
            (json.dumps(_EMPTY_STATE), session_id),
        )


def _save_state(session_id: str, state: dict) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET goal_state = ? WHERE id = ?",
            (json.dumps(state), session_id),
        )


def record_block(session_id: str, verdict: str, feedback: str) -> int:
    """A gate block occurred: bump both counters, stash the verdict, return the
    new consecutive-block count."""
    state = _load_state(session_id)
    state["consecutive_blocks"] = int(state.get("consecutive_blocks", 0)) + 1
    state["total_evals"] = int(state.get("total_evals", 0)) + 1
    state["last_verdict"] = verdict
    state["last_feedback"] = feedback
    _save_state(session_id, state)
    return state["consecutive_blocks"]


def record_pass(session_id: str, verdict: str, feedback: str = "") -> None:
    """The gate allowed the turn to end: bump total_evals, record the verdict,
    and if the goal was achieved deactivate it. consecutive_blocks stays (it is
    reset per new user turn, not per pass)."""
    state = _load_state(session_id)
    state["total_evals"] = int(state.get("total_evals", 0)) + 1
    state["last_verdict"] = verdict
    if feedback:
        state["last_feedback"] = feedback
    if verdict == "achieved":
        state["active"] = False
    _save_state(session_id, state)


def reset_for_new_turn(session_id: str) -> None:
    """Every new user turn zeroes the consecutive-block counter so the cap is
    per-turn, not per-session (Claude Code parity)."""
    state = _load_state(session_id)
    if state.get("consecutive_blocks"):
        state["consecutive_blocks"] = 0
        _save_state(session_id, state)


def is_active(session_id: str) -> bool:
    g = get_goal(session_id)
    return bool(g["goal"]) and bool(g["state"].get("active"))
