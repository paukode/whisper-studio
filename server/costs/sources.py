"""Where a billed model call came from: the ``source`` of a cost-log row.

Every writer names one of these, so the Costs tab can split spend by source
and nothing lands in the log unlabelled. ``unattributed`` exists only for
rows written before sources were recorded (migration 021).
"""

from __future__ import annotations

SOURCE_LABELS: dict[str, str] = {
    "chat": "Chat",
    "agent": "Agents",
    "workflow": "Workflows",
    "cron": "Scheduled tasks",
    "voice": "Voice",
    "headless": "Headless runs",
    "wake": "Replies to agent reports",
    "memory": "Memory",
    "compaction": "Compaction",
    "title": "Titles",
    "query_rewrite": "Query rewrite",
    "btw": "Side questions",
    "classifier": "Auto-mode classifier",
    "explainer": "Permission explainer",
    "evaluator": "Goal evaluator",
    "ci_diagnose": "CI diagnosis",
    "condensation": "Transcript condensation",
    "ocr": "OCR",
    "index": "Workspace index",
    "buddy": "Buddy",
    "doctor": "Doctor checks",
    "unattributed": "Unattributed (recorded before sources)",
}


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source or SOURCE_LABELS["unattributed"])


def require_source(source: str) -> str:
    """The source, validated: an unknown or empty one is a programming error
    that would leave spend unlabelled, so it raises instead of guessing."""
    if source not in SOURCE_LABELS or source == "unattributed":
        raise ValueError(f"unknown cost source {source!r}")
    return source
