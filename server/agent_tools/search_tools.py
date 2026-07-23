"""Executor for tool_search: searches the full catalog and activates matches."""

import json

# Activation guards against re-bloating the pool in one call: at most this
# many tools activate per search, and at most this many bytes of schema are
# returned inline (the rest stay index-only until searched more narrowly).
_ACTIVATE_BATCH_MAX = 5
_SCHEMA_BYTES_MAX = 8_192


def execute_tool_search(tool_input: dict, all_tools: list, session_id: str = "") -> str:
    """Search the FULL tool catalog; matches are activated for this session.

    Query forms: plain keywords (ranked), or ``select:name1,name2`` for exact
    names. Matches return their full input_schema and, unless
    ``activate=false``, register in the session's activation set so they are
    advertised (callable) from the next round onward.
    """
    raw_query = (tool_input.get("query") or "").strip()
    query = raw_query.lower()
    max_results = int(tool_input.get("max_results", 8))
    do_activate = tool_input.get("activate", True) is not False

    matched: list[dict] = []
    if query.startswith("select:"):
        wanted = [n.strip() for n in raw_query[len("select:") :].split(",") if n.strip()]
        by_name = {t.get("name", ""): t for t in all_tools}
        matched = [by_name[n] for n in wanted if n in by_name]
    else:
        scored = []
        for tool in all_tools:
            name = tool.get("name", "")
            desc = tool.get("description", "")
            score = 0
            for word in query.split():
                if word in name.lower():
                    score += 3
                if word in desc.lower():
                    score += 1
            if score > 0:
                scored.append((score, name, tool))
        scored.sort(key=lambda x: (-x[0], x[1]))
        matched = [t for _, _, t in scored[:max_results]]

    # Inline schemas up to the byte budget; beyond it, name+description only.
    matches_out = []
    schema_bytes = 0
    for tool in matched:
        entry = {"name": tool.get("name", ""), "description": tool.get("description", "")[:200]}
        schema = tool.get("input_schema")
        if schema is not None:
            blob = json.dumps(schema)
            if schema_bytes + len(blob) <= _SCHEMA_BYTES_MAX:
                entry["input_schema"] = schema
                schema_bytes += len(blob)
        matches_out.append(entry)

    activated: list[str] = []
    if do_activate and session_id and matched:
        from server.chat.tool_activation import activate

        activated = activate(session_id, [t.get("name", "") for t in matched[:_ACTIVATE_BATCH_MAX]])

    out: dict = {
        "query": raw_query,
        "matches": matches_out,
        "total_tools": len(all_tools),
    }
    if activated:
        out["activated"] = activated
        out["note"] = (
            "These tools are now activated and callable from your NEXT round "
            "(this round's tool list was fixed when it was sent)."
        )
    return json.dumps(out)
