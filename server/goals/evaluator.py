"""The goal judge's prompt, its verdict contract, and the cloud judge.

Two judges share one prompt and one strict parser:

- A cloud-model session is judged by the ``auxiliary_models.goal_evaluator``
  model (Haiku by default; "main" means the session's own model) through
  ``evaluate`` below, one non-streaming call on a worker thread.
- An on-device session is judged by its own resident model, in every mode, by
  ``server.goals.local_judge``. goal_evaluator does not apply to it.

Neither fails open. A judge the model mode forbids, a call that fails, and an
answer that is still unreadable after one retry all return ``not_checked``
with the reason: the turn ends, the goal stays set, and nothing is recorded as
achieved.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable

from server.goals import Verdict, not_checked
from server.goals.tail import render_tail

log = logging.getLogger("whisper-studio")

_MAX_TOKENS = 400

JUDGE_SYSTEM = (
    "You are a strict completion evaluator inside a coding agent's loop. "
    "You are given a GOAL and the tail of the agent's transcript. Decide whether "
    "the goal is genuinely achieved based on EVIDENCE in the transcript, not the "
    "agent's own claims. If the transcript contains a verify_change result, weight "
    "'VERIFY PASS' / 'VERIFY FAIL' above any prose the agent wrote. Be skeptical of "
    "'done!' with no supporting tool output.\n\n"
    "Reply with ONLY a JSON object, no prose, no code fence:\n"
    '{"verdict": "achieved" | "not_achieved" | "blocked", '
    '"feedback": "<one or two sentences: what is left, or why it is blocked>", '
    '"confidence": <0.0-1.0>}\n\n'
    "verdict meanings: 'achieved' = goal is genuinely met; 'not_achieved' = more "
    "work is needed (feedback says what); 'blocked' = the goal cannot be completed "
    "without something outside the agent's control (feedback says what)."
)

RETRY_SUFFIX = "\n\nRespond with ONLY the JSON object."

# The answer shape, for engines that constrain decoding to a JSON schema
# (llama-server). Every engine still goes through parse_verdict.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["achieved", "not_achieved", "blocked"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "feedback": {"type": "string", "maxLength": 400},
    },
    "required": ["verdict", "confidence", "feedback"],
    "additionalProperties": False,
}

# Verdict spellings a judge actually produces, mapped to the contract. Anything
# else is unreadable, never a default.
_VERDICTS = {
    "achieved": "achieved",
    "done": "achieved",
    "complete": "achieved",
    "completed": "achieved",
    "success": "achieved",
    "not_achieved": "not_achieved",
    "incomplete": "not_achieved",
    "in_progress": "not_achieved",
    "blocked": "blocked",
    "stuck": "blocked",
    "cannot": "blocked",
}


def user_prompt(goal: str, tail: str) -> str:
    return f"GOAL:\n{goal.strip()}\n\nTRANSCRIPT TAIL:\n{tail}\n\nJSON verdict:"


def _extract_json(text: str) -> dict | None:
    """Pull the first balanced {...} object out of a model reply and parse it."""
    if not text:
        return None
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    val = json.loads(text[start : i + 1])
                    return val if isinstance(val, dict) else None
                except (ValueError, TypeError):
                    return None
    return None


def parse_verdict(raw: str | None) -> Verdict | None:
    """The verdict in a judge's answer, or None when it is not readable: no
    JSON object, an unknown or missing verdict, or a missing or non-numeric
    confidence. Confidence is clamped to 0..1; feedback may be absent."""
    data = _extract_json(raw or "")
    if data is None:
        return None
    spelled = str(data.get("verdict") or "").strip().lower().replace("-", "_").replace(" ", "_")
    verdict = _VERDICTS.get(spelled)
    conf = data.get("confidence")
    if verdict is None or isinstance(conf, bool):
        return None
    try:
        conf = float(conf)
    except (TypeError, ValueError):
        return None
    if conf != conf:  # NaN
        return None
    feedback = data.get("feedback")
    feedback = "" if feedback is None else str(feedback).strip()
    return Verdict(verdict=verdict, feedback=feedback, confidence=max(0.0, min(1.0, conf)))


def model_label(key: str) -> str:
    """The name the user knows a chat_models key by."""
    try:
        from server.local.runtime import is_local_model, local_model_meta

        if is_local_model(key):
            return local_model_meta(key).get("label") or key
        from server.infrastructure.config import load_config

        meta = (load_config().get("chat_model_meta") or {}).get(key) or {}
        return meta.get("label") or key
    except Exception:  # noqa: BLE001 - a label must never break the judge
        return key


def judge_key(main_model_key: str = "") -> str:
    """The chat_models key that judges a cloud-model session's goal."""
    from server.infrastructure.auxiliary import resolve_task_key

    return resolve_task_key("goal_evaluator", "haiku", main_model_key or None)


def _one_shot(system: str, user: str, main_model_key: str = "", session_id: str = "") -> str:
    """One completion on the goal_evaluator model. Raises on failure."""
    from server.infrastructure.auxiliary import aux_one_shot

    return aux_one_shot(
        "goal_evaluator",
        system,
        user,
        max_tokens=_MAX_TOKENS,
        main_model_key=main_model_key or None,
        session_id=session_id,
    )


def _judge_refusal(main_model_key: str = "") -> str | None:
    """Why the configured judge may not run in this model mode, else None."""
    try:
        from server.infrastructure.auxiliary import aux_refusal

        return aux_refusal("goal_evaluator", main_model_key=main_model_key or None)
    except Exception as e:  # noqa: BLE001 - the one_shot path reports its own failure
        log.debug("goal evaluator refusal check failed: %s", e)
        return None


def evaluate(
    goal: str,
    messages: list,
    *,
    main_model_key: str = "",
    announce: Callable[[str], None] | None = None,
    session_id: str = "",
) -> Verdict:
    """Judge a cloud-model session's goal from the transcript tail on the
    goal_evaluator model. Blocking: run it on a worker thread.

    ``main_model_key`` is the session's model, which "main" resolves to.
    ``announce`` (thread-safe) is told which model is checking, just before
    the call. A judge the mode refuses is never called; it and every failure
    return ``not_checked`` with the reason."""
    refusal = _judge_refusal(main_model_key)
    if refusal:
        return not_checked(refusal)
    label = model_label(judge_key(main_model_key))
    user = user_prompt(goal, render_tail(messages))
    if announce is not None:
        announce(f"Checking the goal on {label}...")
    try:
        verdict = parse_verdict(_one_shot(JUDGE_SYSTEM, user, main_model_key, session_id))
        if verdict is None:
            verdict = parse_verdict(
                _one_shot(JUDGE_SYSTEM, user + RETRY_SUFFIX, main_model_key, session_id)
            )
    except Exception as e:  # noqa: BLE001 - reported to the user, never a pass
        log.info("Goal evaluator failed: %s", e)
        return not_checked(f"The goal evaluator ({label}) could not run: {e}")
    if verdict is None:
        log.info("Goal evaluator answered twice without a readable verdict")
        return not_checked(
            f"The goal evaluator ({label}) answered twice without a readable verdict."
        )
    return verdict


def looks_verified(messages: list) -> bool:
    """True if the transcript tail contains a passing verify_change token, a
    deterministic signal the gate can trust over the evaluator's prose."""
    tail = render_tail(messages)
    return bool(re.search(r"\bVERIFY PASS\b", tail))
