"""FastAPI router for the declarative approval system.

One endpoint:
- POST /api/approval/execute, single hub the frontend calls when the
  user clicks Yes on an approval card. It names the card by its id, and the
  hub runs the action and payload the server recorded for that card, once
  (server/approval/cards.py). Only a `direct` action (one a person starts
  from the app's own UI, with no card) is named by action and payload.
  Returns {ok, output?, error?}.

The frontend does not fetch the category list from the backend. It
seeds its session-memory approval state from a small hardcoded default
(write/delete/cli) and treats any unknown category as "ask" lazily, so
no dynamic categories fetch exists.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import cards, registry
from .spec import execute_approved_by_human

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/approval", tags=["approval"])


class ExecuteRequest(BaseModel):
    # The card the user said Yes to (server/approval/cards.py).
    card_id: str | None = None
    # A `direct` action only: what to run, with no card behind it.
    action: str | None = None
    payload: dict = {}


@router.post("/execute")
async def approval_execute(req: ExecuteRequest):
    if req.card_id:
        taken = cards.take(req.card_id)
        if isinstance(taken, str):
            log.info("approval/execute: card %s refused: %s", req.card_id, taken)
            return {"ok": False, "error": taken}
        try:
            return await _execute(taken.action, _card_payload(taken))
        finally:
            cards.finish(taken.card_id)
    spec = registry.get(req.action or "")
    if spec is None or not spec.direct:
        # Every other action runs only from a card the server showed: a
        # payload posted here is not what any person was asked about.
        log.warning("approval/execute: %r posted without a card", req.action)
        raise HTTPException(status_code=403, detail="This action runs only from an approval card.")
    return await _execute(req.action or "", dict(req.payload or {}))


def _card_payload(card: cards.Card) -> dict:
    payload = dict(card.payload)
    # The provider's id of the call the card paused on. A write keeps the
    # edit under it for the diff dialog (server/workspace/edits.py).
    if card.tool_use_id:
        payload["__tool_use_id__"] = card.tool_use_id
    return payload


async def _execute(action: str, payload: dict) -> dict:
    spec = registry.get(action)
    if not spec:
        log.warning("approval/execute: unknown action %r", action)
        raise HTTPException(status_code=404, detail=f"Unknown approval action: {action}")

    # Snapshot the connected workspace so we can tell the frontend to switch
    # panels if this action changed it (e.g. git_clone with open=true). We
    # detect the switch generically by diffing the path instead of teaching
    # this hub about specific actions, connect_workspace is the only thing
    # that mutates it, so a changed value is an unambiguous "open" signal.
    from server.workspace import get_workspace_path

    ws_before = get_workspace_path()
    # The action runs with the sandbox scope of the turn that raised the
    # card, never the mode live at the click (execute_approved_by_human).
    try:
        outcome = await execute_approved_by_human(spec, payload)
    except Exception as e:  # noqa: BLE001 - executor is user-registered
        log.exception("approval/execute: %s failed", action)
        return {"ok": False, "error": f"Executor crashed: {e}"}

    # Invalidate /api/git/changes cache for write-class categories so the
    # panel sees the new file state on the next refresh. "office-script"
    # belongs here and "save" does not: a script's destination can be inside
    # the connected workspace (dest_kind="workspace"), whereas the save
    # category exists precisely for files landing outside any workspace.
    if outcome.ok and spec.category in ("write", "delete", "cli", "office-script"):
        from server.git.router import invalidate_changes_cache

        invalidate_changes_cache()

    ws_after = get_workspace_path()
    ws_folder_opened = ws_after if (outcome.ok and ws_after and ws_after != ws_before) else None

    return {
        "ok": outcome.ok,
        "output": outcome.output,
        "error": outcome.error,
        "stopped": bool(getattr(outcome, "stopped", False)),
        "ws_folder_opened": ws_folder_opened,
        # A write's diff stats (ApprovalOutcome.stats), the same dict the
        # inline path puts in its skill_result meta; null for other actions.
        "stats": getattr(outcome, "stats", None),
        # A finished command's exit status, the one the inline path's
        # skill_result frame carries; null for every other action.
        "exit_code": getattr(outcome, "exit_code", None),
        # What only the model reads (ApprovalOutcome.model_note): the card's
        # continuation appends it to the tool result, the row never shows it.
        "model_note": getattr(outcome, "model_note", None),
    }
