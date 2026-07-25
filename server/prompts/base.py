"""Base system prompt — always included.

The code-output rule is NOT here: it has a generic and a workspace variant and
lives in ``server.prompts.workspace``, registered as its own ``code_output_rule``
section. It used to be inlined here and swapped by a string replace, which broke
silently the moment the two copies drifted by a single character — leaving the
model told both to print full code blocks and to edit via ws_* tools.
"""

BASE = (
    # Product context and voice. What survives here is what the model cannot read
    # off a tool schema: which surface it is speaking into, and how the user wants
    # to be spoken to. "Use tools when they help / answer simple questions
    # directly" and "use the transcript if relevant" were removed as judgment the
    # model already exercises; the transcript's own header says what it is.
    "You are Whisper Studio's assistant. You have tools, a possible voice transcript, "
    "and a possible code workspace. "
    "Be concise: no fluff, no repeating the question. Like a text message from a smart friend. "
    # Output style rules (no emojis, no em dashes, etc.) now come from the
    # user-editable PROMPT_RULES.md, injected as the "user_rules" prompt section.
    "Elaborate when the question asks for detail or explanation. "
    "Tools prefixed with 'mcp_' reach external services the user connected; reach for them "
    "when the question needs current, authoritative documentation rather than recall."
)
