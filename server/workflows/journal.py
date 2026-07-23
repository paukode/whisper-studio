"""Append-only run journal (journal.jsonl) + the resume cache.

Every workflow run writes one journal under
``data_root()/workflows/runs/<run_id>/journal.jsonl``: run_meta, phase, each
agent_call (with its result), log, error, and done. It is the streamable record
the UI tails and the source of truth for resume.

Resume: ``load_resume_cache(run_id)`` reads a prior run's journal into a map
``call_hash -> FIFO deque of completed results``. A new agent call hashes its
(prompt, opts) the same way (determinism guards make identical upstream inputs
re-hash identically), so a hit replays instantly at zero cost. FIFO/multiset
semantics make it robust to parallel() completion-order differences — the
issue-order hash is what matches.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import deque

from server.infrastructure.paths import data_root


def runs_root() -> str:
    return os.path.join(data_root(), "workflows", "runs")


def run_dir(run_id: str) -> str:
    return os.path.join(runs_root(), run_id)


def journal_path(run_id: str) -> str:
    return os.path.join(run_dir(run_id), "journal.jsonl")


def call_hash(prompt: str, opts: dict | None) -> str:
    """Stable hash of an agent call's (prompt, opts). opts is canonicalized
    (sorted keys) and internal/display-only keys are dropped so a relabel never
    invalidates a cached result."""
    canon = _canonical_opts(opts or {})
    blob = json.dumps({"prompt": prompt, "opts": canon}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


_CACHE_IGNORED_OPTS = {"label", "phase"}


def _canonical_opts(opts: dict) -> dict:
    return {k: v for k, v in sorted(opts.items()) if k not in _CACHE_IGNORED_OPTS}


class Journal:
    """Append-only writer. One instance per run; not thread-safe by design —
    the run coroutine is the sole writer (agent RPCs are serialized through it)."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.path = journal_path(run_id)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def _write(self, entry: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n")

    def run_meta(self, meta: dict) -> None:
        self._write({"type": "run_meta", **meta})

    def phase(self, name: str) -> None:
        self._write({"type": "phase", "name": name})

    def log(self, message: str) -> None:
        self._write({"type": "log", "message": message})

    def agent_call(self, entry: dict) -> None:
        self._write({"type": "agent_call", **entry})

    def error(self, message: str, stack: str = "") -> None:
        self._write({"type": "error", "message": message, "stack": stack})

    def done(self, result) -> None:
        self._write({"type": "done", "result": result})


def read_journal(run_id: str) -> list[dict]:
    """Read all journal entries for a run (empty list if none)."""
    path = journal_path(run_id)
    if not os.path.exists(path):
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except (ValueError, TypeError):
                continue
    return out


def tail_journal(run_id: str, limit: int = 200) -> list[dict]:
    entries = read_journal(run_id)
    return entries[-limit:] if len(entries) > limit else entries


def load_resume_cache(run_id: str) -> dict[str, deque]:
    """Build ``call_hash -> deque([result, ...])`` from a prior run's completed
    agent calls. Ordered by ISSUE order (seq), not journal/completion order: two
    identical-(prompt,opts) parallel calls journal in completion order, but the
    re-run issues them in seq order, so FIFO replay must match seq or the results
    land in swapped positions."""
    cache: dict[str, deque] = {}
    entries = [e for e in read_journal(run_id) if e.get("type") == "agent_call"]
    entries.sort(key=lambda e: e.get("seq", 0))
    for e in entries:
        if e.get("status") not in ("completed", "cache_hit"):
            continue
        h = e.get("call_hash")
        if not h:
            continue
        result = {
            "text": e.get("text", ""),
            "output": e.get("output"),
            "usage": e.get("usage"),
            "status": e.get("status") if e.get("status") != "cache_hit" else "completed",
            "agent_id": e.get("agent_id", ""),
        }
        cache.setdefault(h, deque()).append(result)
    return cache
