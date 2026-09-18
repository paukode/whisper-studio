"""On-disk record of every agent run, written while the run is alive.

An agent's work used to exist in three fragile places: the parent's tool
result (gone once it leaves context), the live registry (gone on restart) and
the task registry row (written only after a normal return, so a cancelled or
crashed member left nothing). The journal makes the record primary:

    <storage_root>/agents/<session_id>/<agent_id>/
        meta.json       task, type, model, budgets, status, stop reason, usage
        events.jsonl    every progress event as it happened
        messages.json   the API message list, rewritten at every round start
        report.md       the final report: the model's own, or a salvage one

The task registry row (server/tasks/registry.py) is created when the run
starts and points at events.jsonl, so ``task_output`` tails a live agent and
returns a finished one's report regardless of how it ended. ``messages.json``
is what lets a stopped agent be resumed with its context intact.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone

from server.infrastructure.paths import storage_root

log = logging.getLogger("whisper-studio")

STOP_REASONS = ("completed", "turn_limit", "deadline", "cost_cap", "cancelled", "error")

# What the parent reads per member and what the registry keeps as result_text.
REPORT_CHARS = 6000

# Appended to the final-round reminder of every agent so the last message is a
# report the parent can act on, not the tail of a narration.
REPORT_TEMPLATE = (
    "<system-reminder>Write your final report now, in this shape:\n"
    "FINDINGS: what you established, most important first.\n"
    "EVIDENCE: where each finding comes from (file, URL, command output).\n"
    "NOT CHECKED: what remains open and why you stopped there.\n"
    "CONFIDENCE: high, medium or low, with the reason in one line.\n"
    "TOUCHED: files written, artifacts created, branches or PRs.\n"
    "ONE LINE: the sentence the caller can quote to the user.\n"
    "The caller has not seen anything you wrote before this message."
    "</system-reminder>"
)

# Told to the parent alongside every agent report (Claude Code's rule for
# subagent output: the user never saw it, so the parent must relay it).
RELAY_NOTE = (
    "These reports were not shown to the user. Relay what matters, say how "
    "confident each report is, and use task_output <agent_id> for a full record."
)

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def agents_root() -> str:
    return os.path.join(storage_root(), "agents")


def _safe(part: str) -> str:
    return _SAFE.sub("_", part or "").strip("_") or "no-session"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_atomic(path: str, text: str) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def find_dir(agent_id: str, session_id: str | None = None) -> str | None:
    """Locate an agent's journal directory; scans every session dir when the
    session is unknown (a resume request only carries the agent id)."""
    if not agent_id or "/" in agent_id or agent_id.startswith("."):
        return None
    root = agents_root()
    if session_id:
        d = os.path.join(root, _safe(session_id), agent_id)
        return d if os.path.isdir(d) else None
    try:
        for sess in os.listdir(root):
            d = os.path.join(root, sess, agent_id)
            if os.path.isdir(d):
                return d
    except OSError:
        return None
    return None


def load(agent_id: str, session_id: str | None = None) -> dict | None:
    """The stored record: meta plus messages and report when present."""
    d = find_dir(agent_id, session_id)
    if not d:
        return None
    out: dict = {"dir": d, "meta": {}, "messages": None, "report": ""}
    try:
        with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
            out["meta"] = json.load(f)
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(d, "messages.json"), encoding="utf-8") as f:
            msgs = json.load(f)
        out["messages"] = msgs if isinstance(msgs, list) else None
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(d, "report.md"), encoding="utf-8") as f:
            out["report"] = f.read()
    except OSError:
        pass
    return out


def salvage_report(
    reason: str,
    *,
    texts: list[str],
    tools_called: list[str],
    turns_used: int,
    error: str = "",
    agent_id: str = "",
) -> str:
    """A report assembled by the runtime when the model never got to write
    one. Deterministic: the last things the agent said, its tool trail, and
    where the full record lives."""
    label = {
        "cancelled": "cancelled before it could report",
        "error": "failed before it could report",
        "cost_cap": "stopped at the cost cap before it could report",
        "deadline": "stopped at the time limit before it could report",
        "turn_limit": "stopped at the turn limit before it could report",
    }.get(reason, f"stopped ({reason}) before it could report")
    lines = [
        f"[Agent {label}. This report was assembled by the runtime from the "
        f"agent's record after {turns_used} tool round(s); the agent did not write it.]"
    ]
    if error:
        lines.append(f"Error: {error[:500]}")
    said = [t.strip() for t in texts if t and t.strip()]
    if said:
        tail = "\n\n".join(said[-3:])
        if len(tail) > 2500:
            tail = "…" + tail[-2500:]
        lines.append("Last things it wrote:\n" + tail)
    else:
        lines.append("It wrote no prose before stopping.")
    if tools_called:
        counts = Counter(tools_called).most_common(10)
        lines.append("Tools used: " + ", ".join(f"{n} x{c}" for n, c in counts))
    if agent_id:
        lines.append(f"Full record: task_output {agent_id}")
    return "\n\n".join(lines)


class AgentJournal:
    """One run's on-disk record plus its task registry row."""

    def __init__(self, agent_id: str, dir_path: str):
        self.agent_id = agent_id
        self.dir = dir_path
        self.events_path = os.path.join(dir_path, "events.jsonl")
        self.messages_path = os.path.join(dir_path, "messages.json")
        self.report_path = os.path.join(dir_path, "report.md")
        self.meta_path = os.path.join(dir_path, "meta.json")
        self._meta: dict = {}
        self._closed = False

    # ── lifecycle ─────────────────────────────────────────────────────────

    @classmethod
    def open(
        cls,
        agent_id: str,
        *,
        session_id: str,
        task: str,
        agent_type: str,
        model: str | None,
        max_turns: int | None = None,
        deadline_seconds: float | None = None,
        parent_agent_id: str | None = None,
        team_id: str | None = None,
        agent_name: str | None = None,
        resumed: bool = False,
    ) -> AgentJournal | None:
        """Create (or reopen) the record and its registry row. Returns None
        when the disk is not writable; callers treat that as "no journal"."""
        d = os.path.join(agents_root(), _safe(session_id), agent_id)
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            log.warning("agent journal: cannot create %s: %s", d, e)
            return None
        j = cls(agent_id, d)
        prev: dict = {}
        if resumed:
            try:
                with open(j.meta_path, encoding="utf-8") as f:
                    prev = json.load(f)
            except (OSError, ValueError):
                prev = {}
        runs = int(prev.get("runs") or 0) + 1
        j._meta = {
            **prev,
            "agent_id": agent_id,
            "session_id": session_id,
            "task": prev.get("task") or (task or "")[:4000],
            "last_message": (task or "")[:4000] if resumed else "",
            "agent_type": agent_type,
            "agent_name": agent_name,
            "model": model or "",
            "parent_agent_id": parent_agent_id,
            "team_id": team_id,
            "max_turns": max_turns,
            "deadline_seconds": deadline_seconds,
            "status": "running",
            "stop_reason": "",
            "runs": runs,
            "started_at": prev.get("started_at") or _now(),
            "resumed_at": _now() if resumed else None,
            "finished_at": None,
            "rounds": int(prev.get("rounds") or 0),
            "usage": prev.get("usage") or {},
        }
        j._save_meta()
        j.event("started" if not resumed else "resumed", task=(task or "")[:2000], model=model)
        try:
            from server.tasks import registry

            registry.ensure_running_task(
                agent_id,
                kind="agent",
                session_id=session_id,
                title=j._meta["task"],
                output_path=j.events_path,
                meta={
                    "agent_type": agent_type,
                    "model": model or "",
                    "journal_dir": d,
                    "team_id": team_id or "",
                    "parent_agent_id": parent_agent_id or "",
                },
            )
        except Exception as e:  # noqa: BLE001 - the registry is a mirror, never the record
            log.debug("agent journal: registry row for %s not written: %s", agent_id, e)
        return j

    def event(self, phase: str, **fields) -> None:
        line = {"ts": _now(), "phase": phase, **fields}
        try:
            with open(self.events_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        except (OSError, TypeError, ValueError) as e:
            log.debug("agent journal: event not written for %s: %s", self.agent_id, e)

    def checkpoint(self, messages: list, round_num: int) -> None:
        """Snapshot the API message list so a killed run can be resumed from
        its last round; called by the runner at every round start."""
        try:
            _write_atomic(self.messages_path, json.dumps(messages, ensure_ascii=False, default=str))
            self._meta["rounds"] = max(int(self._meta.get("rounds") or 0), round_num)
            self._meta["checkpoint_at"] = _now()
            self._save_meta()
        except (OSError, TypeError, ValueError) as e:
            log.debug("agent journal: checkpoint not written for %s: %s", self.agent_id, e)

    def finish(
        self,
        *,
        status: str,
        stop_reason: str,
        report: str,
        turns_used: int,
        usage: dict | None = None,
        messages: list | None = None,
    ) -> None:
        """Write the report and the terminal state. Idempotent: the first
        terminal write wins (a cancelled run's salvage must not be overwritten
        by a later teardown)."""
        if self._closed:
            return
        self._closed = True
        if messages is not None:
            try:
                _write_atomic(
                    self.messages_path, json.dumps(messages, ensure_ascii=False, default=str)
                )
            except (OSError, TypeError, ValueError):
                pass
        try:
            _write_atomic(self.report_path, report or "")
        except OSError as e:
            log.debug("agent journal: report not written for %s: %s", self.agent_id, e)
        self._meta.update(
            status=status,
            stop_reason=stop_reason if stop_reason in STOP_REASONS else "error",
            finished_at=_now(),
            rounds=max(int(self._meta.get("rounds") or 0), turns_used),
            usage=usage or self._meta.get("usage") or {},
        )
        self._save_meta()
        self.event("finished", status=status, stop_reason=stop_reason, turns_used=turns_used)
        try:
            from server.tasks import registry

            registry.finish_task(
                self.agent_id,
                status={"completed": "completed", "stopped": "stopped"}.get(status, "failed"),
                exit_code=None,
                result_text=(report or "")[:REPORT_CHARS],
            )
        except Exception as e:  # noqa: BLE001 - mirror only
            log.debug("agent journal: registry finish for %s failed: %s", self.agent_id, e)

    @property
    def closed(self) -> bool:
        return self._closed

    def _save_meta(self) -> None:
        try:
            _write_atomic(self.meta_path, json.dumps(self._meta, ensure_ascii=False, default=str))
        except (OSError, TypeError, ValueError) as e:
            log.debug("agent journal: meta not written for %s: %s", self.agent_id, e)


def close_quietly(
    journal: AgentJournal | None,
    status: str,
    stop_reason: str,
    report: str,
    *,
    turns_used: int = 0,
    usage: dict | None = None,
    messages: list | None = None,
) -> None:
    """Terminal write to a record; idempotent, never raises into the run."""
    if journal is None or journal.closed:
        return
    try:
        journal.finish(
            status=status,
            stop_reason=stop_reason,
            report=report,
            turns_used=turns_used,
            usage=usage,
            messages=messages,
        )
    except Exception as e:  # noqa: BLE001 - the record must never take the run down
        log.debug("agent journal close failed for %s: %s", journal.agent_id, e)


def team_manifest(session_id: str, team_id: str, payload: dict) -> None:
    """Record which agents make up a team, so a restart can find every
    member's journal from the team id alone."""
    d = os.path.join(agents_root(), _safe(session_id), "teams")
    try:
        os.makedirs(d, exist_ok=True)
        _write_atomic(
            os.path.join(d, f"{_safe(team_id)}.json"),
            json.dumps({**payload, "written_at": _now(), "t": time.time()}, default=str),
        )
    except OSError as e:
        log.debug("agent journal: team manifest not written: %s", e)
