"""Tool-call loop guardrails: pure, per-turn, side-effect free.

The turn engine already bounds a turn by rounds and by the completion-gate
cap, but nothing noticed the model calling the SAME tool with the SAME
arguments over and over, or cycling through the same small batch of calls.
On-device models do this the most; a 50-round turn of identical greps burns
the whole budget and returns nothing new.

This module tracks, per turn scope, every tool call the executor dispatches
and answers two questions:

``before_call``
    May this call run? It is refused (a synthetic tool result, no dispatch)
    when the same call with the same arguments already returned the same
    result twice in a row, when the same call already failed three times
    unchanged, when the last rounds repeated a fixed cycle of calls with
    identical results, or when a per-turn cap for a runaway-prone tool
    (web search, delegation) is exhausted.

``after_call``
    Record the outcome. The second identical result in a row gets a short
    notice appended so the model changes course before the refusal, and a
    byte-identical large payload is replaced by a stub that points at the
    earlier result instead of re-entering the context in full.

Pollers (task_status, ci_status, sleep, ...) legitimately repeat, so they are
exempt from the identical-call checks; caps still apply to them.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field

IDENTICAL_STREAK_LIMIT = 3  # the Nth consecutive identical (tool, args, result) call is refused
FAILURE_STREAK_LIMIT = 3  # the Nth consecutive identical FAILING call is refused
MAX_CYCLE_PERIOD = 4  # A,B,A,B... up to four distinct calls per cycle
CYCLE_REPEATS = 3  # how many full cycles before the next repeat is refused
STUB_MIN_CHARS = 512  # duplicate payloads at or above this size become a stub
_HISTORY_LIMIT = 256

DEFAULT_CAPS: dict[str, int] = {
    "web_search": 50,
    "spawn_agent": 50,
    "team_create": 20,
}

# Tools whose repeated identical invocation is the point (polling a background
# task, waiting, checking CI, re-listing). Exempt from streak and cycle checks.
REPEATABLE_TOOLS: frozenset[str] = frozenset(
    {
        "task_status",
        "task_output",
        "task_list",
        "task_get",
        "sleep",
        "receive_messages",
        "list_agents",
        "ci_status",
        "workflow_status",
        "cron_list",
        "git_status",
        "preview_screenshot",
        "preview_logs",
        "ask_user_question",
    }
)


@dataclass
class GuardDecision:
    allow: bool = True
    reason: str = ""  # "identical_streak" | "repeated_failure" | "cycle" | "cap" | ""
    message: str = ""


@dataclass
class _Call:
    name: str
    args: str  # canonical argument hash
    result: str  # result hash
    error: bool = False  # failures are governed by the repeated-failure rule only


@dataclass
class _TurnState:
    calls: list[_Call] = field(default_factory=list)
    failures: dict[tuple[str, str], int] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    # (name, args) -> the previous byte-identical result hash, for stubbing.
    last_result: dict[tuple[str, str], str] = field(default_factory=dict)


_lock = threading.Lock()
_states: dict[str, _TurnState] = {}
_MAX_SCOPES = 512


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()


def canonical_args(tool_input: dict | None) -> str:
    """Stable hash of a tool call's arguments, ignoring the executor's private
    ``__session_id__`` / ``__agent__`` stamps."""
    if not isinstance(tool_input, dict):
        return _digest(repr(tool_input))
    public = {k: v for k, v in tool_input.items() if not str(k).startswith("__")}
    try:
        return _digest(json.dumps(public, sort_keys=True, default=str))
    except (TypeError, ValueError):
        return _digest(repr(sorted(public.items(), key=lambda kv: str(kv[0]))))


def enabled() -> bool:
    try:
        from server.infrastructure.feature_flags import is_enabled

        return is_enabled("tool_loop_guard")
    except Exception:  # noqa: BLE001 - a config hiccup must never disable tools
        return True


def caps() -> dict[str, int]:
    """Per-turn call caps: DEFAULT_CAPS overlaid with the ``tool_loop_caps``
    config map (a 0 disables a cap)."""
    merged = dict(DEFAULT_CAPS)
    try:
        from server.infrastructure.config import load_config

        raw = load_config().get("tool_loop_caps") or {}
        if isinstance(raw, dict):
            for name, value in raw.items():
                try:
                    merged[str(name)] = int(value)
                except (TypeError, ValueError):
                    continue
    except Exception:  # noqa: BLE001
        pass
    return merged


def reset_for_turn(scope: str) -> None:
    if not scope:
        return
    with _lock:
        _states.pop(scope, None)


def _state(scope: str) -> _TurnState:
    st = _states.get(scope)
    if st is None:
        if len(_states) >= _MAX_SCOPES:
            _states.pop(next(iter(_states)), None)
        st = _TurnState()
        _states[scope] = st
    return st


def _trailing_identical(calls: list[_Call], name: str, args: str) -> int:
    """Length of the trailing run of calls equal to (name, args) whose results
    are all identical to each other."""
    n = 0
    result: str | None = None
    for c in reversed(calls):
        if c.name != name or c.args != args or c.error:
            break
        if result is None:
            result = c.result
        elif c.result != result:
            break
        n += 1
    return n


def _cycle_period(calls: list[_Call], name: str, args: str) -> int | None:
    """If the upcoming call continues a cycle of period p (2..MAX_CYCLE_PERIOD)
    that has already repeated CYCLE_REPEATS times with identical results,
    return p; else None."""
    for p in range(2, MAX_CYCLE_PERIOD + 1):
        need = p * CYCLE_REPEATS
        if len(calls) < need:
            continue
        window = calls[-need:]
        periodic = all(
            (window[i].name, window[i].args, window[i].result)
            == (window[i % p].name, window[i % p].args, window[i % p].result)
            for i in range(need)
        )
        if not periodic:
            continue
        # The cycle must contain more than one distinct call (a plain streak
        # is handled by _trailing_identical) and the upcoming call must be
        # the next element of it.
        distinct = {(c.name, c.args) for c in window[:p]}
        if len(distinct) < 2:
            continue
        nxt = window[-p]
        if nxt.name == name and nxt.args == args:
            return p
    return None


def before_call(scope: str, tool_name: str, tool_input: dict | None) -> GuardDecision:
    """Decide whether a tool call may run. Never raises."""
    if not scope or not enabled():
        return GuardDecision()
    args = canonical_args(tool_input)
    with _lock:
        st = _state(scope)
        st.counts[tool_name] = st.counts.get(tool_name, 0) + 1
        cap = caps().get(tool_name, 0)
        if cap and st.counts[tool_name] > cap:
            return GuardDecision(
                allow=False,
                reason="cap",
                message=(
                    f"[Loop guard] {tool_name} has been called {cap} times this turn, which is "
                    "the per-turn cap. This looks like a runaway loop. Work with the results you "
                    "already have and give the user your answer."
                ),
            )
        fails = st.failures.get((tool_name, args), 0)
        if fails >= FAILURE_STREAK_LIMIT:
            return GuardDecision(
                allow=False,
                reason="repeated_failure",
                message=(
                    f"[Loop guard] Blocked {tool_name}: the same call failed {fails} times with "
                    "identical arguments. Repeating it unchanged will fail again. Change the "
                    "arguments, use a different tool, or explain the blocker to the user."
                ),
            )
        if tool_name in REPEATABLE_TOOLS:
            return GuardDecision()
        streak = _trailing_identical(st.calls, tool_name, args)
        if streak >= IDENTICAL_STREAK_LIMIT - 1 and streak > 0:
            return GuardDecision(
                allow=False,
                reason="identical_streak",
                message=(
                    f"[Loop guard] Stopped {tool_name}: the same call with identical arguments "
                    f"already returned the same result {streak} times in a row. The result is "
                    "above in this conversation; use it, change the arguments, or change "
                    "strategy instead of repeating the call."
                ),
            )
        period = _cycle_period(st.calls, tool_name, args)
        if period:
            return GuardDecision(
                allow=False,
                reason="cycle",
                message=(
                    f"[Loop guard] Stopped {tool_name}: the last {period * CYCLE_REPEATS} tool "
                    f"calls repeated the same cycle of {period} calls with identical arguments and "
                    "identical results. Repeating the batch unchanged is not progress; change "
                    "the approach or report what blocks you."
                ),
            )
    return GuardDecision()


def after_call(
    scope: str,
    tool_name: str,
    tool_input: dict | None,
    output: str,
    *,
    is_error: bool,
) -> tuple[str | None, str | None]:
    """Record an executed call. Returns ``(notice, stub)``: ``notice`` is text
    to append to the result when the model is one repeat away from a refusal;
    ``stub`` replaces the whole output when it is a large byte-identical
    duplicate of the previous same-args result."""
    if not scope or not enabled():
        return None, None
    args = canonical_args(tool_input)
    text = output if isinstance(output, str) else str(output)
    result = _digest(text)
    with _lock:
        st = _state(scope)
        key = (tool_name, args)
        if is_error:
            st.failures[key] = st.failures.get(key, 0) + 1
        else:
            st.failures.pop(key, None)
        prev_result = st.last_result.get(key)
        st.last_result[key] = result
        st.calls.append(_Call(tool_name, args, result, error=is_error))
        if len(st.calls) > _HISTORY_LIMIT:
            del st.calls[: len(st.calls) - _HISTORY_LIMIT]
        if tool_name in REPEATABLE_TOOLS:
            return None, None
        streak = _trailing_identical(st.calls, tool_name, args)
    notice = None
    stub = None
    if streak >= 2:
        notice = (
            f"\n\n[Loop guard note: this is consecutive identical call number {streak} to "
            f"{tool_name} with identical arguments and the same result. Do not repeat it; "
            "change the arguments, use a different tool, or proceed with what you have.]"
        )
        if prev_result == result and len(text) >= STUB_MIN_CHARS:
            stub = (
                f"[Identical to the previous {tool_name} result with the same arguments "
                f"({len(text)} chars, not repeated). Refer to that earlier result.]"
            )
    return notice, stub


__all__ = [
    "CYCLE_REPEATS",
    "DEFAULT_CAPS",
    "FAILURE_STREAK_LIMIT",
    "IDENTICAL_STREAK_LIMIT",
    "MAX_CYCLE_PERIOD",
    "REPEATABLE_TOOLS",
    "STUB_MIN_CHARS",
    "GuardDecision",
    "after_call",
    "before_call",
    "canonical_args",
    "caps",
    "reset_for_turn",
]
