"""``workspace_graph_query`` — the chat model's typed-relationship lookup tool.

Read-only, concurrent-safe. Given an entity name, returns the typed facts about it
(who it works with, what it owns, where it is located, …) aggregated across files,
each with a source citation the user can open in the side panel. This is the
assistant-queryable face of the knowledge graph — distinct from
``workspace_semantic_search``, which retrieves passages by meaning.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import quote

from server.executors import register_executor
from server.workspace import get_workspace_path

from . import paths
from .citations import citation_link

log = logging.getLogger("whisper-studio")


def _cite(ws: str, path: str, start: int | None, end: int | None) -> str:
    """A #wsfile citation for a fact's source. Uses the line anchor when the
    relation carried line provenance, else a file-level link."""
    href = os.path.normpath(os.path.join(ws, path)) if ws and not path.startswith("/") else path
    if start:
        return citation_link(path, start, end or start, href)
    return f"[{path}](#wsfile={quote(href)})"


@register_executor("workspace_graph_query", read_only=True, concurrent_safe=True)
def exec_workspace_graph_query(tool_input, transcript, current_attachments):
    ws = get_workspace_path()
    if not ws:
        return "No workspace connected."
    if not paths.is_indexed(ws):
        return (
            "This workspace has not been indexed yet, so the knowledge graph is "
            "unavailable. Ask the user to index it (and enable typed relations) "
            "from the workspace dialog. Until then, use workspace_semantic_search."
        )
    entity = (tool_input.get("entity") or "").strip()
    if not entity:
        return "Error: entity is required."
    predicate = (tool_input.get("predicate") or "").strip() or None
    limit = tool_input.get("limit") or 15
    try:
        limit = max(1, min(int(limit), 40))
    except (TypeError, ValueError):
        limit = 15

    from . import relstore

    facts = relstore.facts_for_entity(ws, entity, limit=limit, predicate=predicate)
    if not facts:
        return (
            f"No typed relationships found for {entity!r}. Typed relations must be "
            "enabled and indexed for this workspace; otherwise use "
            "workspace_semantic_search to find passages mentioning it."
        )

    lines = [f"Typed relationships for {entity!r}:", ""]
    for f in facts:
        arrow = "→" if f["direction"] == "out" else "←"
        cite = f.get("cite") or {}
        line = (
            f"- {arrow} {f['predicate']} {f['other']} "
            f"(confidence {f['score']:.2f}, {f['sources']} source(s))"
        )
        if cite.get("path"):
            line += "  " + _cite(ws, cite["path"], cite.get("start_line"), cite.get("end_line"))
        lines.append(line)
        if cite.get("evidence"):
            lines.append(f'    "{cite["evidence"]}"')
    lines.append("")
    lines.append(
        "Cite these facts by copying the [path:lines](#wsfile=...) links exactly, "
        "and end your answer with a 'Sources' section listing them."
    )
    return "\n".join(lines)
