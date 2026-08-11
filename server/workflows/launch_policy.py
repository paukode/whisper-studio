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

from server.workflows.runtime import DEFAULT_WORKFLOW_BUDGET_TOKENS

# "auto" approves a run only when its budget is within the stock default; a
# script asking for more than 600k output tokens is exactly the kind of run
# the card exists for.
WORKFLOW_AUTO_MAX_TOKENS = DEFAULT_WORKFLOW_BUDGET_TOKENS


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
