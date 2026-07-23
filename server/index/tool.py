"""``workspace_semantic_search`` — the chat model's semantic retrieval tool.

Read-only, concurrent-safe, returns a plain string (the contract every workspace
tool follows). Vector search over the workspace index plus one GraphRAG hop, so
the model finds conceptually-related code without exact keywords. Falls back
with a clear message when the workspace isn't connected or hasn't been indexed
yet (the model can't index — that's a user action in the workspace dialog).
"""

from __future__ import annotations

import logging
import os

from server.executors import register_executor
from server.workspace import get_workspace_path

from . import paths
from .citations import citation_link

log = logging.getLogger("whisper-studio")

_SNIPPET_CHARS = 240


def _snippet(text: str) -> str:
    s = " ".join(text.split())
    return s[:_SNIPPET_CHARS] + ("…" if len(s) > _SNIPPET_CHARS else "")


def _ref(rel_path: str, start: int, end: int, ws: str = "") -> str:
    """A markdown source link the chat UI turns into an open-in-side-panel action.

    Display text stays workspace-relative; the ``#wsfile=`` href is absolute (so
    it resolves regardless of the connected workspace) and carries the cited line
    range, which the client scrolls to. Cmd/Ctrl-click reveals in Finder."""
    href = os.path.normpath(os.path.join(ws, rel_path)) if ws else rel_path
    return citation_link(rel_path, start, end, href)


@register_executor("workspace_semantic_search", read_only=True, concurrent_safe=True)
def exec_workspace_semantic_search(tool_input, transcript, current_attachments):
    ws = get_workspace_path()
    if not ws:
        return "No workspace connected."
    if not paths.is_indexed(ws):
        return (
            "This workspace has not been indexed yet, so semantic search is "
            "unavailable. Ask the user to index it from the workspace dialog "
            "(Connect Workspace → enable indexing). Until then, use ws_grep/ws_glob."
        )

    query = (tool_input.get("query") or "").strip()
    if not query:
        return "Error: query is required."
    limit = tool_input.get("limit") or 8
    try:
        limit = max(1, min(int(limit), 25))
    except (TypeError, ValueError):
        limit = 8

    from .pipeline import query as run_query

    res = run_query(ws, query, k=limit)
    matches = res.get("matches", [])
    related = res.get("related", [])
    if not matches:
        return f"No semantic matches for {query!r}."

    lines = [f"Semantic matches for {query!r}:", ""]
    for i, m in enumerate(matches, 1):
        lines.append(
            f"{i}. {_ref(m['path'], m['start_line'], m['end_line'], ws)}  (score {m['score']:.2f})"
        )
        lines.append(f"   {_snippet(m['text'])}")
    if related:
        lines.append("")
        lines.append("Related via shared entities (GraphRAG hop):")
        for r in related:
            lines.append(
                f"- {_ref(r['path'], r['start_line'], r['end_line'], ws)}  "
                f"(shares {r.get('shared_entities', 0)} entities)"
            )
    lines.append("")
    lines.append(
        "When you use any of this, cite the source files by copying the markdown "
        "links exactly as given above, including the #wsfile=...&L=start-end "
        "fragment; clicking one opens the file at the cited lines in the side "
        "panel (Cmd/Ctrl-click reveals it in Finder). Always end your answer with "
        "a 'Sources' section: a short list with one such link per line for every "
        "file you drew on."
    )
    return "\n".join(lines)
