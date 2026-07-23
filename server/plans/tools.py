"""The create_plan tool schema. Execution lives in server/tool_router.py
(writes the markdown via server.plans.store and emits a `plan_generated`
side-effect), mirroring create_artifact's tool -> card side-effect pattern."""

from __future__ import annotations

PLAN_TOOLS = [
    {
        "name": "create_plan",
        "description": (
            "Save a detailed plan as a document that opens in the side pane. Call this "
            "whenever the user asks for a plan (or you are in plan mode): put the FULL "
            "detailed plan in `markdown`, a one-line `summary`, and a short `title`. "
            "Do NOT paste the full plan into your reply — after calling this, reply with "
            "ONLY the one-line summary. The plan is saved and shown to the user in the pane."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short plan title, a few words"},
                "summary": {"type": "string", "description": "One-line summary shown in the chat"},
                "markdown": {
                    "type": "string",
                    "description": "The full detailed plan, in markdown",
                },
            },
            "required": ["title", "summary", "markdown"],
        },
    },
]

PLAN_TOOL_NAMES = {t["name"] for t in PLAN_TOOLS}
