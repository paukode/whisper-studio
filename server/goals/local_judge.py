"""The on-device goal judge.

An on-device session is judged by its own resident model, in Local and Hybrid
mode alike, so nothing about its goal is ever sent to Bedrock.
``auxiliary_models.goal_evaluator`` names the judge for cloud-model sessions
only and is not consulted here. The call goes through
``serving.complete_on_resident``, which is safe inside the turn that holds
the model's busy mark: it never loads, displaces or waits.

A small model judging its own work is a weaker signal than a test (``/goal
gate add`` is the strong one), so every verdict says who judged it, and any
failure is a visible ``not_checked``, never a pass. The transcript tail is
sized to the resident context window, and a window too small for the check is
refused before anything is sent.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from server.goals import Verdict, not_checked
from server.goals.evaluator import (
    JUDGE_SYSTEM,
    RETRY_SUFFIX,
    VERDICT_SCHEMA,
    model_label,
    parse_verdict,
    user_prompt,
)
from server.goals.tail import DEFAULT_CAP_CHARS, ELIDED_PREFIX, render_tail

log = logging.getLogger("whisper-studio")

# Room for the verdict (at most 400 feedback chars) plus a short preamble on
# an engine that does not constrain its output.
MAX_TOKENS = 768
TIMEOUT_S = 300.0
# Below this much transcript the judge would be guessing.
MIN_TAIL_CHARS = 2_000
# Chars per token, deliberately low for code and tool output, and the tokens
# kept free for the chat template and estimate error.
_CHARS_PER_TOKEN = 3
_SLACK_TOKENS = 512
_DEFAULT_CTX = 32768

_aux_note_logged = False


def tail_budget_chars(n_ctx: int, goal: str) -> int:
    """Characters of transcript tail that fit a judge prompt (retry included)
    in an ``n_ctx`` window, at most render_tail's default cap."""
    prompt_chars = (n_ctx - MAX_TOKENS - _SLACK_TOKENS) * _CHARS_PER_TOKEN
    fixed = len(JUDGE_SYSTEM) + len(user_prompt(goal, "")) + len(RETRY_SUFFIX) + len(ELIDED_PREFIX)
    return min(DEFAULT_CAP_CHARS, prompt_chars - fixed)


def _window(model_key: str) -> int:
    """The resident context window: llama-server's real --ctx-size, or MLX's
    recorded size, else the registry's, else the 32K default."""
    from server.local import serving
    from server.local.runtime import local_model_meta

    n = serving.resident_n_ctx() if serving.resident_key() == model_key else None
    n = n or local_model_meta(model_key).get("ctx")
    try:
        return int(n) if n else _DEFAULT_CTX
    except (TypeError, ValueError):
        return _DEFAULT_CTX


def _note_unused_aux_key() -> None:
    global _aux_note_logged
    if _aux_note_logged:
        return
    from server.infrastructure.auxiliary import aux_model_key

    configured = aux_model_key("goal_evaluator", "")
    if configured:
        _aux_note_logged = True
        log.info(
            "auxiliary_models.goal_evaluator=%r applies to cloud-model sessions only; "
            "an on-device session is judged by its own model",
            configured,
        )


async def evaluate_on_device(
    goal: str,
    messages: list,
    *,
    model_key: str,
    announce: Callable[[str], None] | None = None,
) -> Verdict:
    """Judge an on-device session's goal on its own resident model.

    ``announce`` is told which model is checking, only when a request is
    about to be sent. One retry on an unreadable answer; a transport failure,
    a cut-off or empty answer is not retried (temperature 0 repeats it).
    ``TIMEOUT_S`` bounds the answer; time spent queued behind another
    chat's or agent's round on the same server is allowed for on top (see
    ``serving.complete_on_resident``)."""
    from server.local import serving

    label = model_label(model_key)
    _note_unused_aux_key()

    def failed(reason: str) -> Verdict:
        return not_checked(f"{label} could not check the goal: {reason}")

    n_ctx = _window(model_key)
    cap = tail_budget_chars(n_ctx, goal)
    if cap < MIN_TAIL_CHARS:
        return failed(
            f"its context window ({n_ctx:,} tokens) is too small for the goal check; "
            "raise the context size in the composer"
        )
    user = user_prompt(goal, render_tail(messages, cap_chars=cap))
    # The judged turn holds one busy mark. Any other is another live turn (a
    # chat or an agent) on the same one-slot server, whose round the check
    # may wait behind.
    shared = serving.turns_besides(1) > 0
    if announce is not None:
        wait = " (it is also answering another chat or agent, so this may wait)" if shared else ""
        announce(f"Checking the goal on {label}{wait}...")
    verdict = None
    try:
        for prompt in (user, user + RETRY_SUFFIX):
            raw = await serving.complete_on_resident(
                model_key,
                JUDGE_SYSTEM,
                prompt,
                max_tokens=MAX_TOKENS,
                json_schema=VERDICT_SCHEMA,
                timeout=TIMEOUT_S,
                held_marks=1,
            )
            verdict = parse_verdict(raw)
            if verdict is not None:
                break
    except serving.ResidentCallError as e:
        return failed(str(e))
    except Exception as e:  # noqa: BLE001 - reported to the user, never a pass
        log.warning("On-device goal judge failed: %s", e)
        return failed(f"{type(e).__name__}: {e}")
    if verdict is None:
        return failed("it answered twice without a readable verdict")
    verdict.note = f"Judged by {label} on this Mac:"
    return verdict
