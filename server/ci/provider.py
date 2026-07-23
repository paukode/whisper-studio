"""Read-only ``gh`` CLI wrapper for GitHub Actions state.

Every call here is a query — never a mutation — so watching or diagnosing CI
can't change the repo. Subprocess hygiene mirrors ``server/git/core._run_git``:
stdin closed, no interactive prompt, bounded timeout. A missing/failed ``gh``
degrades to an empty result the caller can branch on, never an exception that
crashes a poll loop.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess

log = logging.getLogger("whisper-studio")

# A run is only worth acting on once it has settled.
TERMINAL_STATUSES = {"completed"}
FAILING_CONCLUSIONS = {"failure", "timed_out", "startup_failure", "action_required"}


def gh_available() -> bool:
    return shutil.which("gh") is not None


def _run_gh(args: list[str], cwd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run one ``gh`` subcommand. Never raises for the caller — a non-zero exit
    is returned as-is so callers read returncode/stderr and degrade."""
    exe = shutil.which("gh") or "gh"
    env = _gh_env()
    try:
        return subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("ci.provider: gh %s failed: %s", args[:2], e)
        return subprocess.CompletedProcess(args, returncode=124, stdout="", stderr=str(e))


def _gh_env() -> dict:
    import os

    env = os.environ.copy()
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_NO_UPDATE_NOTIFIER"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["CLICOLOR"] = "0"
    env.pop("GH_PAGER", None)
    env["PAGER"] = "cat"
    return env


def _json(proc: subprocess.CompletedProcess):
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except (ValueError, TypeError):
        return None


_RUN_FIELDS = "databaseId,status,conclusion,workflowName,headBranch,headSha,url,createdAt,event"


def list_runs(branch: str, cwd: str, *, limit: int = 10) -> list[dict]:
    """Recent Actions runs for a branch, newest first (normalized dicts)."""
    proc = _run_gh(
        [
            "run",
            "list",
            "--branch",
            _safe_branch(branch),
            "--limit",
            str(limit),
            "--json",
            _RUN_FIELDS,
        ],
        cwd,
    )
    rows = _json(proc)
    if not isinstance(rows, list):
        return []
    return [_norm_run(r) for r in rows]


def latest_run(branch: str, cwd: str) -> dict | None:
    runs = list_runs(branch, cwd, limit=1)
    return runs[0] if runs else None


def get_run(run_id: int | str, cwd: str) -> dict | None:
    proc = _run_gh(
        ["run", "view", str(run_id), "--json", _RUN_FIELDS + ",jobs"],
        cwd,
    )
    data = _json(proc)
    if not isinstance(data, dict):
        return None
    run = _norm_run(data)
    run["jobs"] = [_norm_job(j) for j in data.get("jobs", []) or []]
    return run


def failed_jobs(run: dict) -> list[dict]:
    """The jobs of an already-fetched run (from :func:`get_run`) that failed."""
    return [j for j in run.get("jobs", []) if j.get("conclusion") in FAILING_CONCLUSIONS]


def failing_log(run_id: int | str, cwd: str, *, max_bytes: int = 24_000) -> str:
    """The `--log-failed` output for a run: only the steps that failed. Tail-
    sliced to ``max_bytes`` so a giant log can't blow the diagnosis prompt, and
    secret-scrubbed as a second layer beyond GitHub's own masking before the
    text ever reaches the model or the session."""
    proc = _run_gh(["run", "view", str(run_id), "--log-failed"], cwd, timeout=60)
    text = scrub_secrets(proc.stdout or "")
    if len(text) > max_bytes:
        text = "…(head elided)…\n" + text[-max_bytes:]
    return text


# Conservative, prefix-anchored patterns — deliberately NOT broad entropy
# matching (which would redact real error text). Each replaces the secret value
# with a marker while keeping surrounding log context intact.
_SECRET_PATTERNS = [
    re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\b(gh[posru]|github_pat)_[A-Za-z0-9_]{20,}\b"),  # GitHub tokens/PAT
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),  # Slack tokens
    re.compile(r"\b(sk|rk)-[A-Za-z0-9]{20,}\b"),  # OpenAI/Stripe-style keys
    # key/token/secret/password = <value> assignments (env-dump style)
    re.compile(
        r"(?i)\b([A-Z0-9_]*(?:secret|token|password|passwd|api[_-]?key|access[_-]?key))\b(\s*[=:]\s*)"
        r"['\"]?([^\s'\"]{6,})['\"]?"
    ),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{12,}"),  # Authorization: Bearer …
    re.compile(
        r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----"
    ),
]

_REDACTED = "***REDACTED***"


def scrub_secrets(text: str) -> str:
    if not text:
        return text
    out = text
    for pat in _SECRET_PATTERNS:
        if pat.groups >= 3:  # keep the key name + separator, redact the value
            out = pat.sub(lambda m: m.group(1) + m.group(2) + _REDACTED, out)
        else:
            out = pat.sub(_REDACTED, out)
    return out


def pr_for_branch(branch: str, cwd: str) -> dict | None:
    # `--` ends flag parsing so a branch value can't be read as a gh flag.
    proc = _run_gh(
        ["pr", "view", "--json", "number,title,url,state,headRefName", "--", _safe_branch(branch)],
        cwd,
    )
    data = _json(proc)
    return data if isinstance(data, dict) else None


def _safe_branch(branch: str) -> str:
    """Reject a branch value that would be parsed as a gh flag. Git refs can't
    begin with '-' anyway, so a leading dash is always bogus/hostile input."""
    b = (branch or "").strip()
    return "HEAD" if b.startswith("-") else b


def is_failing(run: dict | None) -> bool:
    return bool(run) and run.get("conclusion") in FAILING_CONCLUSIONS


def is_terminal(run: dict | None) -> bool:
    return bool(run) and run.get("status") in TERMINAL_STATUSES


def _norm_run(r: dict) -> dict:
    return {
        "run_id": r.get("databaseId"),
        "status": r.get("status"),
        "conclusion": r.get("conclusion"),
        "workflow": r.get("workflowName"),
        "branch": r.get("headBranch"),
        "sha": r.get("headSha"),
        "url": r.get("url"),
        "event": r.get("event"),
        "created_at": r.get("createdAt"),
    }


def _norm_job(j: dict) -> dict:
    return {
        "name": j.get("name"),
        "status": j.get("status"),
        "conclusion": j.get("conclusion"),
        "url": j.get("url"),
    }
