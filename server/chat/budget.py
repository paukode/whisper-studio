"""Tool-result truncation: persist oversize outputs to the result cache and
send the model a head+tail slice so the conversation stays under the Bedrock
prompt cap while losing as little as possible.

The truncation is surfaced as an SSE ``tool_result_truncated`` event so
the chat UI can render a "view full output" affordance, instead of the
data silently disappearing.

The closure pattern (``make_budget_tool_result``) lets callers collect
truncation events into a per-request list — the chat endpoint flushes
those as SSE frames; tests use the simpler ``_budget_tool_result``
non-emitting variant.
"""

import logging

from server.infrastructure import result_cache

log = logging.getLogger("whisper-studio")

# Feature 3: Tool result budget — if a tool result exceeds this size,
# persist the full output and send a head+tail slice plus a reference.
TOOL_RESULT_BUDGET_BYTES = 50_000

# What survives into the model's context when over budget. Head carries most
# of the signal for typical outputs (JSON payloads, listings); the tail keeps
# trailing summaries/instructions from being lost outright. Head + tail +
# framing stays under TOOL_RESULT_BUDGET_BYTES so the result needs no second
# pass through the budgeter.
HEAD_KEEP_CHARS = 40_000
TAIL_KEEP_CHARS = 8_000


def _budget_tool_result(tool_name: str, tool_output: str) -> str:
    """
    Feature 3: If tool output exceeds budget, persist to disk and return a reference.

    Backwards-compatible non-emitting variant. For UI surfacing, prefer
    ``make_budget_tool_result(events)`` which records truncation events into a
    per-request list so the chat handler can flush them as SSE events.
    """
    return make_budget_tool_result(None)(tool_name, tool_output)


def _sliced(tool_output: str) -> str:
    """Head+tail slice of an over-budget output with an omission marker."""
    head = tool_output[:HEAD_KEEP_CHARS]
    tail = tool_output[-TAIL_KEEP_CHARS:]
    omitted = len(tool_output) - len(head) - len(tail)
    return f"{head}\n\n[... {omitted:,} characters omitted ...]\n\n{tail}"


def make_budget_tool_result(events: list[dict] | None):
    """Build a budgeter closure that, when truncation happens, appends a
    ``tool_result_truncated`` event description to ``events`` (if provided).
    The caller is responsible for translating those into SSE frames.
    """

    def _budget(tool_name: str, tool_output: str) -> str:
        full_size = len(tool_output.encode("utf-8"))
        if full_size <= TOOL_RESULT_BUDGET_BYTES:
            return tool_output

        kept = _sliced(tool_output)
        try:
            fname = result_cache.write(tool_name, tool_output)
            log.info(
                "Tool result budgeted: %s -> %s (%d bytes)", tool_name, fname, len(tool_output)
            )
            if events is not None:
                events.append(
                    {
                        "tool_result_truncated": {
                            "tool_name": tool_name,
                            "full_size": full_size,
                            "kept_bytes": len(kept.encode("utf-8")),
                            "cache_filename": fname,
                            "cache_path": result_cache.relative_path(fname),
                        }
                    }
                )
            return (
                f"{kept}\n\n"
                f"[Output truncated to head+tail — full result ({len(tool_output):,} chars) "
                f"saved to {result_cache.relative_path(fname)}]\n"
                f"To read the omitted middle, call read_cached_result with filename "
                f"'{fname}' and offset/limit."
            )
        except Exception as e:
            log.warning("Tool result cache write failed for %s: %s", tool_name, e)
            if events is not None:
                events.append(
                    {
                        "tool_result_truncated": {
                            "tool_name": tool_name,
                            "full_size": full_size,
                            "kept_bytes": len(kept.encode("utf-8")),
                            "cache_filename": None,
                            "cache_path": None,
                        }
                    }
                )
            return f"{kept}\n\n[Output truncated to head+tail; full result could not be cached]"

    return _budget
