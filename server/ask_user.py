"""
AskUserQuestion tool — pauses the stream and presents a question with
multiple-choice options to the user. The frontend renders a question
widget; the user's selection is sent as the next chat message.

This module owns the tool *descriptors* only. The actual handling
flows through SSE side-effects from server/tool_router.py and the
chat stream — there's no HTTP endpoint to mount.
"""

import logging

log = logging.getLogger("whisper-studio")


# ── Tool definitions ──────────────────────────────────────────────────────────

ASK_USER_TOOL = {
    "name": "ask_user_question",
    "description": (
        "Ask the user a question with multiple-choice options to gather information, "
        "clarify ambiguity, understand preferences, or get a decision. "
        "The conversation will pause until the user responds. "
        "Use this whenever you need user input before proceeding — folder names, "
        "style preferences, approval, feature choices, etc. "
        "Always include an 'Other (please specify)' option."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to ask the user",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of choices. Always add 'Other (please specify)' as the last option.",
            },
        },
        "required": ["question", "options"],
    },
}

SLEEP_TOOL = {
    "name": "sleep",
    "description": (
        "Wait for a specified number of seconds. Use when waiting for a background process, "
        "rate-limiting retries, or when the user asks you to pause. "
        "Maximum 30 seconds per call."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "seconds": {
                "type": "number",
                "description": "Seconds to wait (max 30)",
            },
        },
        "required": ["seconds"],
    },
}

CREATE_ARTIFACT_TOOL = {
    "name": "create_artifact",
    "description": (
        "Create a self-contained artifact and render it as an inline card in the "
        "chat with a title, description, live preview button, and download button. "
        "Use this after generating a complete single-file HTML document. The html "
        "field must be a complete, standalone HTML document with <!DOCTYPE html>. "
        "For a new request to build an app, program, tool, game, or dashboard, "
        "prefer the create_program skill first so the user can choose a single "
        "page or a modular project; use create_artifact once the HTML is ready."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Display title for the artifact (e.g. 'Todo App')",
            },
            "html": {
                "type": "string",
                "description": "Complete HTML source code (must be a full standalone document with <!DOCTYPE html>)",
            },
            "description": {
                "type": "string",
                "description": "Brief 1-2 sentence description of what the artifact does",
            },
        },
        "required": ["title", "html"],
    },
}

ALL_TOOLS = [ASK_USER_TOOL, SLEEP_TOOL, CREATE_ARTIFACT_TOOL]
ALL_TOOL_NAMES = {t["name"] for t in ALL_TOOLS}
