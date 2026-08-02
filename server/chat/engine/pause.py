"""The unified paused-turn store.

One process-wide map of session_id -> paused conversation state, shared by
every provider path. A pause happens when a round's tool batch contains an
approval-gated tool or an ask_user_question: the engine stashes the canonical
messages plus placeholder tool_results, ends the stream, and the continuation
turn (approved_tool_result) restores from here.

In-memory by design: a backend restart drops in-flight pauses, which the
frontend already treats as a resettable state (chat ⋯ menu).
"""

# session_id -> {"messages": [...], "pending_tool_results": [...],
#                "provider": "anthropic" | "openai" | "local", ...}
paused_sessions: dict[str, dict] = {}
