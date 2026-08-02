"""Local-mode tool glue: Anthropic tool schemas in OpenAI function shape.

The engine assembles the tool catalog (progressive disclosure included) and
the LocalAdapter converts it with ``_to_openai_fn`` — llama-server expects the
OpenAI function format. Execution goes through the engine's shared safety
pipeline (``execute_tool_batch`` + ``process_tool_results``); the old
``run_tool_round``/``get_tool_schemas`` wrappers that predated the engine are
gone with the loops that used them.

SAFETY INVARIANT (load-bearing — do not break):
    Destructive tools (write / delete / cli) never mutate anything inside the
    executor. Their handlers return a ``[WS_APPROVAL]`` sentinel string that is
    ONLY honoured by ``process_tool_results`` (not by ``execute_tool_batch``).
    So every tool result MUST flow through ``process_tool_results`` and a
    returned ``has_pending_approval`` MUST be treated as a hard stop — the
    turn engine does exactly this. The one write path is the frontend's
    ``/api/approval/execute`` endpoint (model-agnostic), which the approval
    card calls on "Yes".

The local path stays fully offline: the local TurnContext passes
``tool_exec_model_id=""`` so the permission LLM explainer never fires (it is
gated on a truthy model id), and the auto-mode classifier falls back to asking
instead of calling Bedrock.
"""

from __future__ import annotations

import logging

log = logging.getLogger("whisper-studio")


def _to_openai_fn(anthropic_tool: dict) -> dict:
    """An Anthropic tool ({name, description, input_schema}) → an OpenAI
    function schema. ``input_schema`` is already plain JSON Schema, so it drops
    straight into ``parameters``."""
    return {
        "type": "function",
        "function": {
            "name": anthropic_tool["name"],
            "description": anthropic_tool.get("description", ""),
            "parameters": anthropic_tool.get("input_schema")
            or {"type": "object", "properties": {}},
        },
    }
