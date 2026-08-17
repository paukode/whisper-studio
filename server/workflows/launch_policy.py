"""Category-mode gate for launching NEW (not-yet-trusted) workflow scripts.

The workflow preview card is not an ApprovalSpec tool, so launches don't go
through server/approval. This module reads the SAME permissions.json the rest
of the approval system uses: the "workflow" entry in category_modes, falling
back to the global mode (server/security/permissions.py exposes the category
in GET /api/permissions so the settings UI renders it like any other).

  bypassPermissions -> launch with no card
  auto              -> launch with no card while the run's token budget is at
                       most WORKFLOW_AUTO_MAX_TOKENS; bigger budgets still ask
  dontAsk           -> decline the launch (the model is told why)
  anything else     -> ask (today's preview card)

Trusted saved workflows and resumes never come through here; they launch
directly, as before. The server-side enforcement layer (concurrency, agent
cap, token budget, the vm jail) is untouched by any of these modes.
"""

from __future__ import annotations

import threading

from server.workflows.runtime import DEFAULT_WORKFLOW_BUDGET_TOKENS

# "auto" approves a run only when its budget is within the stock default; a
# script asking for more than 600k output tokens is exactly the kind of run
# the card exists for.
WORKFLOW_AUTO_MAX_TOKENS = DEFAULT_WORKFLOW_BUDGET_TOKENS

# ---------------------------------------------------------------------------
# Session grants: "the user already approved this exact script this session".
#
# Approving a preview card grants the script's hash for that session, so
# re-running it (by name or as the same inline script) launches without a
# second card. Process memory on purpose: a restart clears every grant. The
# DURABLE form is saved-workflow trust, which the same approval sets when the
# script byte-matches a saved workflow (see routes.launch_run). Keyed by
# script hash — the same identity durable trust pins — so any edit to the
# script brings the card back. Same lifetime pattern as the latched session
# config (server/infrastructure/config.py) and the auto-mode breaker
# (server/security/permissions.py).
# ---------------------------------------------------------------------------

_session_grants: dict[str, set[str]] = {}
_grants_lock = threading.Lock()


def _grant_key(script: str) -> str:
    """One keying rule for every grant site. Stripped before hashing because
    the launch route strips the script it receives while saved workflows keep
    their trailing newline — without this, approving a script from the card
    would not cover running the byte-identical saved file by name."""
    from server.workflows.store import script_hash

    return script_hash((script or "").strip())


def grant_session(session_id: str, script: str) -> None:
    """Record that `session_id` approved this exact script."""
    if not session_id or not (script or "").strip():
        return
    with _grants_lock:
        _session_grants.setdefault(session_id, set()).add(_grant_key(script))


def is_session_granted(session_id: str, script: str) -> bool:
    if not session_id or not (script or "").strip():
        return False
    with _grants_lock:
        return _grant_key(script) in _session_grants.get(session_id, ())


def drop_session_grants(session_id: str) -> None:
    """Forget a session's grants (wired into the delete_session cleanup)."""
    with _grants_lock:
        _session_grants.pop(session_id, None)


def launch_decision(budget_tokens: int | None) -> str:
    """Resolve "auto" | "ask" | "deny" for a new-script launch."""
    from server.security.permissions import (
        MODE_AUTO,
        MODE_BYPASS,
        MODE_DONT_ASK,
        VALID_MODES,
        load_permissions,
    )

    data = load_permissions()
    mode = data.get("mode") or "default"
    category_mode = (data.get("category_modes") or {}).get("workflow")
    if category_mode in VALID_MODES:
        mode = category_mode

    if mode == MODE_BYPASS:
        return "auto"
    if mode == MODE_DONT_ASK:
        return "deny"
    if (
        mode == MODE_AUTO
        and budget_tokens is not None
        and budget_tokens <= WORKFLOW_AUTO_MAX_TOKENS
    ):
        return "auto"
    return "ask"
