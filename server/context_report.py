"""What the model is actually being sent, and what is wasteful about it.

Context rots quietly. A prompt section costs tokens on every request whether or
not anyone reads it, guidance drifts into both the system prompt and a tool
description, static text ends up on a per-request layer, and a section can name a
tool that no longer exists. None of that fails a test or raises an exception —
the only symptom is a slightly worse, slightly more expensive model.

So measure it: per-section token cost split by whether it is cached, the
tool-schema split, phrases duplicated between the prompt and a tool description,
and tool names the prompt references but the catalog does not contain.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("whisper-studio")

# Length of the word window used to spot duplicated prose. Short enough to catch
# a restated rule, long enough that ordinary shared phrasing ("the user asks
# you to") does not register.
SHINGLE_WORDS = 8

# Report at most this many duplication findings, longest first.
MAX_FINDINGS = 25

_WORD_RE = re.compile(r"[a-z0-9_]+")

# Any snake_case identifier. Matching by known prefix (ws_, git_, …) was tried
# first and was worse than useless: it could not see `enter_worktree`, the very
# dead reference this check exists to catch. So match broadly and subtract the
# handful of snake_case words that are parameters or prose rather than tools.
_TOOL_REF_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

# Any whitespace-free run containing a slash: a filesystem path, a URL, or a
# file reference. Dropped before scanning for tool names, because the prompt
# interpolates the real workspace path and a directory like `my_project` is not
# a dead tool reference.
_PATHLIKE_RE = re.compile(r"\S*/\S*")

# Add to this when a false positive shows up. Keeping it small is the point: a
# long denylist means the pattern is wrong.
_NOT_TOOLS = frozenset(
    {
        # tool parameters and enum values that appear in prompt prose
        "session_id",
        "in_progress",
        "old_string",
        "new_string",
        "replace_all",
        "run_in_background",
        "file_path",
        "output_mode",
        "files_with_matches",
        "no_ff",
        # prose
        "read_only",
        "launch_json",
    }
)


def _words(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def _shingles(words: list[str], n: int = SHINGLE_WORDS) -> set[tuple[str, ...]]:
    if len(words) < n:
        return set()
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def _overlap_runs(words: list[str], reference: set[tuple[str, ...]], n: int = SHINGLE_WORDS):
    """Contiguous stretches of `words` that also appear in `reference`.

    Yields (phrase, word_count). Adjacent matching windows are merged, so a
    wholesale restatement reports as one long finding rather than dozens of
    overlapping eight-word ones.
    """
    if len(words) < n:
        return
    hits = [i for i in range(len(words) - n + 1) if tuple(words[i : i + n]) in reference]
    if not hits:
        return
    start = prev = hits[0]
    for i in hits[1:]:
        if i == prev + 1:
            prev = i
            continue
        yield " ".join(words[start : prev + n]), prev + n - start
        start = prev = i
    yield " ".join(words[start : prev + n]), prev + n - start


def find_duplication(sections: dict[str, str], tools: list[dict]) -> list[dict]:
    """Phrases a prompt section shares with some tool's description.

    Each finding is guidance being paid for twice: once per request in the
    prompt, and again in the schema of the tool it describes. The tool
    description is the copy to keep — it loads only when the tool does.
    """
    tool_shingles: list[tuple[str, set[tuple[str, ...]]]] = []
    for tool in tools:
        desc = tool.get("description") or ""
        shingles = _shingles(_words(desc))
        if shingles:
            tool_shingles.append((tool.get("name", "?"), shingles))

    findings: list[dict] = []
    for name, text in sections.items():
        words = _words(text)
        for tool_name, shingles in tool_shingles:
            for phrase, count in _overlap_runs(words, shingles):
                findings.append(
                    {
                        "section": name,
                        "tool": tool_name,
                        "words": count,
                        "phrase": phrase if len(phrase) <= 240 else phrase[:240] + "…",
                    }
                )
    findings.sort(key=lambda f: f["words"], reverse=True)
    return findings[:MAX_FINDINGS]


def find_missing_tool_refs(sections: dict[str, str], catalog_names: set[str]) -> list[dict]:
    """Tool names the prompt instructs the model to call that do not exist.

    This is the enter_worktree failure: an ApprovalSpec, a permissions entry, and
    400 tokens per turn of instructions, but no schema and no route, so the
    instruction was unreachable and the protection it described never applied.
    """
    missing: list[dict] = []
    for name, text in sections.items():
        scannable = _PATHLIKE_RE.sub(" ", text or "")
        for ref in sorted(set(_TOOL_REF_RE.findall(scannable))):
            if ref in catalog_names or ref in _NOT_TOOLS:
                continue
            missing.append({"section": name, "tool": ref})
    return missing


def build_context_report(
    *,
    ws_path: str | None = None,
    session_id: str = "doctor",
    question: str = "",
) -> dict:
    """Per-section prompt cost, tool-schema cost, and context-hygiene findings."""
    from server.chat.tool_index import estimate_tool_tokens
    from server.chat.tool_pool import assemble_partitioned_pool
    from server.prompts import PromptLayer, get_registry
    from server.whisper_md import get_whisper_md_context

    registry = get_registry()
    whisper_md = get_whisper_md_context(ws_path, question)

    kwargs = {"ws_path": ws_path, "session_id": session_id, "deferred_tool_index": ""}
    injected = {"whisper_md": whisper_md, "memory_context": "", "session_memory_context": ""}
    sections: list[dict] = []
    texts: dict[str, str] = {}
    for section in registry.get_sections():
        if not section.enabled:
            continue
        # These sections are content-injected per request by the caller,
        # not resolved by the registry. Reading the registry here would
        # report whatever the LAST chat turn left behind (its recalled
        # memories, its session summary) as if it were the shipped prompt.
        # Substitute fresh values: the real WHISPER.md, and empty memory
        # blocks, because a doctor report has no chat turn to recall for.
        text = injected[section.name] if section.name in injected else section.resolve(**kwargs)
        if not text:
            continue
        cached = int(section.layer) <= int(PromptLayer.WORKSPACE)
        texts[section.name] = text
        sections.append(
            {
                "name": section.name,
                "layer": int(section.layer),
                "layer_name": PromptLayer(section.layer).name,
                "cached": cached,
                "chars": len(text),
                "tokens_est": len(text) // 4,
            }
        )

    total_chars = sum(s["chars"] for s in sections)
    cached_chars = sum(s["chars"] for s in sections if s["cached"])

    advertised, deferred, _core = assemble_partitioned_pool(
        ws_connected=bool(ws_path), session_id=session_id
    )
    catalog_names = {t["name"] for t in advertised} | {t["name"] for t in deferred}

    duplication = find_duplication(texts, advertised + deferred)
    missing_refs = find_missing_tool_refs(texts, catalog_names)

    warnings: list[str] = []
    if duplication:
        dup_tokens = sum(f["words"] for f in duplication) * 4 // 3
        warnings.append(
            f"{len(duplication)} phrase(s) duplicated between the prompt and a tool "
            f"description (~{dup_tokens} tokens per request). Keep the tool description."
        )
    for ref in missing_refs:
        warnings.append(
            f"section '{ref['section']}' tells the model to use '{ref['tool']}', "
            "which is not in the tool catalog."
        )
    uncached = total_chars - cached_chars
    if total_chars and uncached * 100 // total_chars > 50:
        warnings.append(
            f"{uncached * 100 // total_chars}% of the prompt is on a per-request layer. "
            "Static text belongs at WORKSPACE or above so it can be cached."
        )

    return {
        "workspace": ws_path,
        "sections": sections,
        "totals": {
            "chars": total_chars,
            "tokens_est": total_chars // 4,
            "cached_chars": cached_chars,
            "cached_tokens_est": cached_chars // 4,
            "cached_pct": (cached_chars * 100 // total_chars) if total_chars else 0,
        },
        "tools": {
            "advertised": len(advertised),
            "advertised_tokens_est": estimate_tool_tokens(advertised),
            "deferred": len(deferred),
            "deferred_tokens_est": estimate_tool_tokens(deferred),
        },
        "duplication": duplication,
        "missing_tool_refs": missing_refs,
        "warnings": warnings,
    }
