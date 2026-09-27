"""Cron completion verification.

An unattended run has no user to notice it stopped early, so before pushing a
result as ``ok`` we ask the cheap evaluator whether the job prompt was actually
satisfied. Synchronous (safe on the cron worker thread). The caller owns the
continuation-budget bookkeeping and the [UNVERIFIED] annotation.
"""

from __future__ import annotations

import logging

from server.goals import Verdict, not_checked

log = logging.getLogger("whisper-studio")

MAX_CONTINUATIONS = 2


def verify(
    job_prompt: str,
    messages: list,
    notifications: list[str] | None = None,
    *,
    main_model_key: str = "",
    session_id: str = "",
) -> Verdict:
    """Judge whether a cron run met its prompt. The job prompt IS the goal.

    ``notifications`` are captured notify_user bodies: they are the run's
    actual deliverable channel, but they live in side-effects rather than the
    transcript, so they are folded in as a synthetic assistant message or a
    report delivered via notify_user would be wrongly judged missing.
    ``main_model_key`` is the run's model, which a goal_evaluator of "main"
    resolves to.

    A verifier that cannot run or cannot be understood returns ``not_checked``
    with the reason, never a pass: the caller spends no continuation on it and
    reports the run as unverified."""
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
        return evaluate(job_prompt, judged, main_model_key=main_model_key, session_id=session_id)
    except Exception as e:  # noqa: BLE001 - reported on the run, never a pass
        log.info("Cron verify failed: %s", e)
        return not_checked(f"the verifier failed ({type(e).__name__}: {e})")
