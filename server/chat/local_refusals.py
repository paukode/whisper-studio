"""Local-mode refusals for the chat side paths that build their own model call.

A chat turn refuses a cloud model in its own model resolution (routes.py,
through ``model_mode.turn_model_refusal``). ``/subagent`` and ``/btw`` reach a
model without going through a chat turn, so each asks here first. A refusal is
the existing error frame as a one-frame SSE reply: the client is already reading
a stream, so an HTTP status alone would never reach the user.
"""

from __future__ import annotations

from fastapi.responses import StreamingResponse

from server.utils import ndjson_dumps

BTW_LOCAL_REFUSAL = (
    "/btw runs on a cloud model, and Local mode keeps chat on this Mac, so it was "
    "not sent. Switch Settings > Model mode to Hybrid or Cloud to use /btw."
)


def sse_error(message: str) -> StreamingResponse:
    """A one-frame SSE reply carrying the existing error frame, then [DONE]."""

    async def _frames():
        yield f"data: {ndjson_dumps({'error': message})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(_frames(), media_type="text/event-stream")


def subagent_refusal(model_key: str) -> StreamingResponse | None:
    """In Local mode a /subagent task runs only on an installed on-device model:
    a cloud key, or a key the catalog does not know, is refused with a reason
    instead of reaching the cloud default. None when the task may run."""
    from server.chat.infra import _get_chat_model_meta, _get_chat_models
    from server.infrastructure.model_mode import current_mode, turn_model_refusal
    from server.local.runtime import is_local_model

    if current_mode() != "local":
        return None
    key = model_key if model_key in _get_chat_models() else ""
    refusal = turn_model_refusal(
        key,
        on_device=is_local_model(key),
        label=(_get_chat_model_meta().get(key) or {}).get("label", ""),
        mode="local",
    )
    return sse_error(refusal) if refusal else None


def btw_refusal() -> StreamingResponse | None:
    """/btw always runs on Bedrock, so Local mode refuses it rather than sending
    the recent messages out. None when it may run."""
    from server.infrastructure.model_mode import current_mode

    return sse_error(BTW_LOCAL_REFUSAL) if current_mode() == "local" else None
