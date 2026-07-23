"""Deferred-tool index: the compact catalog the model discovers tools from.

One line per deferred tool (name plus the first sentence of its description,
truncated), headed by a short instruction to load tools via tool_search.
Deterministic (sorted) so it lives in the prompt-cached static block without
churning the prefix; the index only changes when the catalog itself changes,
which busts the tools cache block anyway.
"""

import json


def _first_sentence(text: str, limit: int = 100) -> str:
    s = (text or "").strip().split("\n")[0]
    for stop in (". ", "; "):
        idx = s.find(stop)
        if idx > 0:
            s = s[: idx + 1]
            break
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def build_deferred_index(deferred: list[dict]) -> str:
    """The system-prompt block listing deferred tools, or '' when none."""
    if not deferred:
        return ""
    lines = [
        "## Additional tools (not loaded)",
        "These tools exist but their schemas are not loaded into this request. "
        "Call tool_search with a keyword query (or select:name1,name2 for exact "
        "names) to load them; matches become callable on your next round.",
        "",
    ]
    for tool in sorted(deferred, key=lambda t: t["name"]):
        lines.append(f"- {tool['name']} — {_first_sentence(tool.get('description', ''))}")
    return "\n".join(lines)


def estimate_tool_tokens(tools: list[dict]) -> int:
    """Ballpark token cost of a tool list (4 chars per token heuristic)."""
    if not tools:
        return 0
    try:
        return len(json.dumps(tools)) // 4
    except (TypeError, ValueError):
        return 0
