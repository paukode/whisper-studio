"""Cron completion verification.

An unattended run has no user to notice it stopped early, so before pushing a
result as ``ok`` we ask the cheap evaluator whether the job prompt was actually
satisfied. Synchronous (safe on the cron worker thread). The caller owns the
continuation-budget bookkeeping and the [UNVERIFIED] annotation.
"""

from __future__ import annotations

import logging

from server.goals import Verdict

log = logging.getLogger("whisper-studio")

MAX_CONTINUATIONS = 2


def verify(job_prompt: str, messages: list, notifications: list[str] | None = None) -> Verdict:
    """Judge whether a cron run met its prompt. The job prompt IS the goal.

    ``notifications`` are captured notify_user bodies — they are the run's
    actual deliverable channel, but they live in side-effects rather than the
    transcript, so they are folded in as a synthetic assistant message or a
    report delivered via notify_user would be wrongly judged missing.

    Returns an achieved verdict on any evaluator failure so verification never
    turns a good run into a spurious failure."""
    try:
        from server.goals.evaluator import evaluate

        judged = list(messages)
        if notifications:
            delivered = "\n\n".join(n for n in notifications if (n or "").strip())
            if delivered:
                judged.append(
                    {
                        "role": "assistant",
                        "content": f"[delivered via notify_user]\n{delivered}",
                    }
                )
        return evaluate(job_prompt, judged, provider="anthropic")
    except Exception as e:  # never let verification itself fail the run
        log.info("Cron verify unavailable: %s", e)
        return Verdict(verdict="achieved", feedback="(verifier unavailable)", confidence=0.0)
