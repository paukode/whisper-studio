"""Goal + goal_state persistence on the sessions row.

A goal is 1:1 with a session, so it lives on two additive columns (migration
009 in sessions._MIGRATED_SESSION_COLUMNS) rather than a new table. goal_state
is a small JSON blob holding the live counters and last verdict:

    {"active": bool, "consecutive_blocks": int, "total_evals": int,
     "last_verdict": str, "last_feedback": str, "set_at": iso8601}
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

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
        cur = conn.execute(
            "UPDATE sessions SET goal = ?, goal_state = ? WHERE id = ?",
            (goal, json.dumps(state), session_id),
        )
        if cur.rowcount == 0:
            # A brand-new session: the frontend creates its row on the first
            # save, which can land AFTER /goal on an empty session. The UPDATE
            # then matched nothing and the goal was silently lost while the
            # route still reported ok. Insert the placeholder row the same way
            # sessions._append_message_sync does; the next save fills in the
            # title and metadata without touching goal/goal_state.
            now = set_at or datetime.now(timezone.utc).isoformat()
            try:
                conn.execute(
                    "INSERT INTO sessions (id, title, custom_title, generated_title, "
                    "created_at, updated_at, segments, chat_history, speaker_names, "
                    "goal, goal_state) "
                    "VALUES (?, 'New Session', 0, 0, ?, ?, '[]', '[]', '{}', ?, ?)",
                    (session_id, now, now, goal, json.dumps(state)),
                )
            except sqlite3.IntegrityError:
                # Another writer inserted the row in the gap; apply the goal to it.
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


# ── Quality gates (shell commands that must exit 0 before the judge runs) ────
#
# Stored on goal_state["gates"] as [{"command": str, "failures": int}, ...].
# They survive /resume and compaction with the goal, and are cleared with it.


def get_gates(session_id: str) -> list[dict]:
    gates = _load_state(session_id).get("gates")
    if not isinstance(gates, list):
        return []
    out = []
    for g in gates:
        if isinstance(g, dict) and str(g.get("command") or "").strip():
            out.append({"command": str(g["command"]), "failures": int(g.get("failures") or 0)})
        elif isinstance(g, str) and g.strip():
            out.append({"command": g.strip(), "failures": 0})
    return out


def add_gate(session_id: str, command: str) -> list[dict]:
    command = (command or "").strip()
    state = _load_state(session_id)
    gates = get_gates(session_id)
    if command and command not in {g["command"] for g in gates}:
        gates.append({"command": command, "failures": 0})
    state["gates"] = gates
    _save_state(session_id, state)
    return gates


def remove_gate(session_id: str, index: int) -> list[dict]:
    """Remove the gate at 1-based ``index``; out-of-range is a no-op."""
    state = _load_state(session_id)
    gates = get_gates(session_id)
    if 1 <= index <= len(gates):
        gates.pop(index - 1)
    state["gates"] = gates
    _save_state(session_id, state)
    return gates


def clear_gates(session_id: str) -> None:
    state = _load_state(session_id)
    state["gates"] = []
    _save_state(session_id, state)


def record_gate_results(session_id: str, results: dict[str, bool]) -> list[dict]:
    """Bump the failure counter of every gate that failed, reset the ones that
    passed. Returns the updated list."""
    state = _load_state(session_id)
    gates = get_gates(session_id)
    for g in gates:
        passed = results.get(g["command"])
        if passed is True:
            g["failures"] = 0
        elif passed is False:
            g["failures"] = int(g.get("failures") or 0) + 1
    state["gates"] = gates
    _save_state(session_id, state)
    return gates


def pause_goal(session_id: str, reason: str = "") -> None:
    """Deactivate the goal without clearing it (an exhausted gate, a cap)."""
    state = _load_state(session_id)
    state["active"] = False
    state["last_verdict"] = "paused"
    if reason:
        state["last_feedback"] = reason
    _save_state(session_id, state)
