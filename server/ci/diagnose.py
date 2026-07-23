"""Turn a run's failing logs into structured, actionable findings.

One cheap Haiku pass (``one_shot``) reads the ``--log-failed`` output plus the
failed job names and returns a findings list: which check failed, its category,
the key error, suspect files, and a suggested fix. Degrades to an empty list
(never raises) so a diagnosis miss just means "no autofix proposed", not a crash.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger("whisper-studio")

_MAX_TOKENS = 1400

_SYSTEM = (
    "You are a CI failure triage assistant. You are given the failing-step logs "
    "of a GitHub Actions run and the names of the jobs that failed. Identify each "
    "distinct failure and how to fix it. Respond with ONLY a JSON object of the form "
    '{"findings":[{"check":"<job/step>","category":"lint|type|test|build|format|other",'
    '"summary":"<one line>","error_excerpt":"<key error lines, <=400 chars>",'
    '"suspect_files":["path", ...],"suggested_fix":"<concrete minimal fix>"}]}. '
    "Group by root cause: one finding per distinct failure, not one per log line. "
    'If the logs show no actionable failure, return {"findings":[]}.'
)

_CATEGORIES = {"lint", "type", "test", "build", "format", "other"}


def _extract_json(text: str):
    """Parse the JSON object OR array a model reply carries. Returns a dict or
    list, else None.

    Robust to two things the naive brace-counter got wrong: (a) the reply is
    already pure JSON (fast path — no scan), and (b) a ``}``/``]`` INSIDE a
    string value (CI error excerpts are full of them: ``Type '{ x }'``, dict
    reprs, JSON). The scan is string-aware — braces inside a JSON string, and
    escaped quotes, don't affect nesting depth."""
    if not text:
        return None
    stripped = text.strip()
    # (a) already pure JSON — the model was asked for ONLY the object.
    try:
        val = json.loads(stripped)
        if isinstance(val, (dict, list)):
            return val
    except (ValueError, TypeError):
        pass
    # (b) scan for the first {...} or [...], string-aware.
    opens = {"{": "}", "[": "]"}
    start = next((i for i, c in enumerate(text) if c in opens), -1)
    if start == -1:
        return None
    close = opens[text[start]]
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == text[start]:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                try:
                    val = json.loads(text[start : i + 1])
                    return val if isinstance(val, (dict, list)) else None
                except (ValueError, TypeError):
                    return None
    return None


def _build_user(run: dict, log_text: str, failed_job_names: list[str]) -> str:
    jobs = ", ".join(failed_job_names) or "(unknown)"
    workflow = run.get("workflow") or "CI"
    return (
        f"Workflow: {workflow}\nFailed jobs: {jobs}\n"
        f"Run URL: {run.get('url', '')}\n\n"
        f"--- failing logs ---\n{log_text or '(no logs captured)'}\n--- end logs ---"
    )


def _one_shot(system: str, user: str) -> str | None:
    try:
        from server.infrastructure.oneshot import one_shot

        return one_shot(system, user, max_tokens=_MAX_TOKENS, cloud_model_key="haiku")
    except Exception as e:  # noqa: BLE001
        log.info("CI diagnose one_shot unavailable: %s", e)
        return None


def _coerce_findings(data) -> list[dict]:
    # Accept both the wrapper object {"findings":[...]} and a bare [...] array
    # (some models drop the wrapper), so a valid diagnosis is never discarded.
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        raw = data.get("findings")
    else:
        return []
    if not isinstance(raw, list):
        return []
    out = []
    for f in raw:
        if not isinstance(f, dict):
            continue
        cat = str(f.get("category", "other")).lower()
        out.append(
            {
                "check": str(f.get("check", ""))[:120],
                "category": cat if cat in _CATEGORIES else "other",
                "summary": str(f.get("summary", ""))[:300],
                "error_excerpt": str(f.get("error_excerpt", ""))[:400],
                "suspect_files": [str(p)[:200] for p in (f.get("suspect_files") or [])][:12],
                "suggested_fix": str(f.get("suggested_fix", ""))[:600],
            }
        )
    return out


def diagnose(run: dict, log_text: str, *, failed_job_names: list[str] | None = None) -> list[dict]:
    """Blocking (one_shot); wrap in ``asyncio.to_thread`` from the loop. Returns
    a list of findings (possibly empty)."""
    names = failed_job_names or [j.get("name", "") for j in run.get("jobs", [])]
    raw = _one_shot(_SYSTEM, _build_user(run, log_text, names))
    data = _extract_json(raw or "")
    if data is None and raw:
        # one retry with a stricter instruction, mirroring the goal evaluator.
        raw = _one_shot(
            _SYSTEM, _build_user(run, log_text, names) + "\n\nRespond with ONLY the JSON."
        )
        data = _extract_json(raw or "")
    return _coerce_findings(data)
