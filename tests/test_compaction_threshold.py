"""Constant-sanity checks for the compaction thresholds in
server/chat/compaction.py.

The chat loop compacts proactively when the estimated message size exceeds
COMPACT_TRIGGER_CHARS, before a request would hit Bedrock's 200K-token input
cap (~800K chars at the codebase's 4-chars/token estimate). These tests pin
both thresholds below that cap so
proactive compaction actually fires, instead of every long conversation
falling through to a billed PromptTooLongError.
"""

from server.chat.compaction import COMPACT_TRIGGER_CHARS, MAX_CONTEXT_CHARS

# Bedrock input cap: 200K tokens * 4 chars/token.
BEDROCK_INPUT_CAP_CHARS = 800_000


def test_compact_trigger_below_bedrock_cap():
    # The whole point of the fix: the proactive trigger sits below the cap so
    # compaction fires before the reactive PromptTooLongError path.
    assert 0 < COMPACT_TRIGGER_CHARS < BEDROCK_INPUT_CAP_CHARS


def test_max_context_below_bedrock_cap():
    # The simple-truncation fallback trims down to MAX_CONTEXT_CHARS, so it too
    # must stay under the cap or the fallback itself would overflow the request.
    assert 0 < MAX_CONTEXT_CHARS <= BEDROCK_INPUT_CAP_CHARS


def test_hard_ceiling_at_or_above_trigger():
    # Today's invariant: MAX_CONTEXT_CHARS (the last-resort truncation ceiling)
    # sits at or above COMPACT_TRIGGER_CHARS (the proactive trigger). Compaction
    # is attempted at the trigger; the ceiling is the safety net nearer the cap.
    assert MAX_CONTEXT_CHARS >= COMPACT_TRIGGER_CHARS
