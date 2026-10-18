"""The ``meta`` object of a skill_result frame: the numbers the UI shows next
to a tool row, read once in the gate (server/tool_executor.py) from what it
already knows about the call.

Every key is always present, null when unknown, so the client schema
(src/types/schemas/chat.schema.ts) can declare them all:

  duration_ms   how long route_tool took (ToolState.duration_ms); null for a
                call that never ran (refused, skipped, blocked)
  output_chars  the length of the budgeted output in full, which is what the
                model received, not the 2000-character preview the UI shows
  truncated     whether the tool-result budget cut the output
  lines         ws_read_file only: the numbered lines the full output
                carries (null for the dedup stub, an error, and a message
                that holds no file text such as "File not found")
  bytes         the size of the file after a write
  added         lines added by a write
  removed       lines removed by a write

The write numbers come from the outcome stats of an inline-approved action
(ApprovalOutcome.stats, computed in server/approval/executors.py _do_write),
and are null for every other call. The card path carries the same stats in
the JSON of POST /api/approval/execute, and the client patches its row. The
stats also hold the line count of the written file, which stays out of the
meta: ``lines`` has one meaning, the lines a read returned.
"""

from __future__ import annotations

import re

META_KEYS = ("duration_ms", "output_chars", "truncated", "lines", "bytes", "added", "removed")

# One line of a ws_read_file result: a right-aligned line number, a tab, the
# text (server/workspace/executors.py). The token-budget notes between the
# head and the tail of a large file carry no number and do not count.
_NUMBERED_LINE_RE = re.compile(r"^ *\d+\t", re.MULTILINE)
# What ws_read_file returns for a file unchanged since the same read
# (server/file_state.py check_dedup): it carries no lines to count.
_DEDUP_STUB_PREFIX = "(file previously read"


def read_file_lines(output: str) -> int | None:
    """The numbered lines a ws_read_file result carries; None for the dedup
    stub, whose line count belongs to the earlier read, and for a message
    with no numbered line ("File not found: x", "Binary file: x", "No
    workspace connected.", an outside-roots refusal). An empty string is an
    empty file (or a range past its end): zero lines."""
    if output.startswith(_DEDUP_STUB_PREFIX):
        return None
    count = len(_NUMBERED_LINE_RE.findall(output))
    if count == 0 and output:
        return None
    return count


def result_meta(state, full_output: str, sent_output: str, *, is_error: bool) -> dict:
    """The meta of ``state``'s skill_result frame. ``full_output`` is the
    result before the budget, ``sent_output`` the text the model receives
    (the same string when nothing was cut)."""
    stats = getattr(state, "stats", None) or {}
    lines = None
    if state.tool_name == "ws_read_file" and not is_error:
        lines = read_file_lines(full_output)
    return {
        "duration_ms": getattr(state, "duration_ms", None),
        "output_chars": len(sent_output),
        "truncated": sent_output != full_output,
        "lines": lines,
        "bytes": stats.get("bytes"),
        "added": stats.get("added"),
        "removed": stats.get("removed"),
    }
