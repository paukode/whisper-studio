"""Memory recall — selects relevant memories at query time via Haiku side-query.

At each user query:
1. Scan both memory tiers (global always; project when a workspace is open)
2. If <=5 files total, return all (skip LLM)
3. Else call Haiku with a merged, tier-tagged manifest to select up to 5
4. Read selected files and return a formatted context block

Entry point is ``recall_memory_context`` which handles tier resolution,
selection, and formatting in one call.
"""

import json
import logging
import os
from datetime import datetime, timezone

import boto3
from botocore.config import Config as BotoConfig

from server.memory.memdir import (
    SCOPE_GLOBAL,
    SCOPE_PROJECT,
    ensure_global_memory_dir,
    ensure_memory_dir,
    load_memory_index,
)
from server.memory.prompts import RECALL_SYSTEM_PROMPT
from server.memory.scan import MemoryFile, scan_memory_files

log = logging.getLogger("whisper-studio")

MAX_SELECTIONS = 5
MAX_RECALL_TOKENS = 256


def _resolve_tiers(ws_path: str | None) -> list[tuple[str, str]]:
    """Return [(scope, memory_dir)] for the tiers alive right now."""
    tiers: list[tuple[str, str]] = []
    global_dir = ensure_global_memory_dir()
    if global_dir:
        tiers.append((SCOPE_GLOBAL, global_dir))
    project_dir = ensure_memory_dir(ws_path)
    if project_dir:
        tiers.append((SCOPE_PROJECT, project_dir))
    return tiers


def _scan_tiers(tiers: list[tuple[str, str]]) -> list[tuple[str, MemoryFile]]:
    """Scan every tier; returns [(scope, MemoryFile)] newest first."""
    entries: list[tuple[str, MemoryFile]] = []
    for scope, memory_dir in tiers:
        for f in scan_memory_files(memory_dir):
            entries.append((scope, f))
    entries.sort(key=lambda e: e[1].mtime, reverse=True)
    return entries


def _entry_key(scope: str, f: MemoryFile) -> str:
    """Stable tier-qualified key ('global/user_role.md'). Filenames may repeat
    across tiers, so the bare filename is not unique."""
    return f"{scope}/{f.filename}"


def _build_tier_manifest(entries: list[tuple[str, MemoryFile]]) -> str:
    """Manifest with tier-qualified keys for the selector LLM."""
    if not entries:
        return "(no memory files)"
    lines = []
    for scope, m in entries:
        tag = f"[{m.type}] " if m.type else ""
        ts = datetime.fromtimestamp(m.mtime, tz=timezone.utc).strftime("%Y-%m-%d")
        desc = f": {m.description}" if m.description else ""
        lines.append(f"- {tag}{_entry_key(scope, m)} ({ts}){desc}")
    return "\n".join(lines)


async def recall_memory_context(
    query: str, ws_path: str | None, *, model_id: str
) -> tuple[str, int]:
    """Select relevant memories across both tiers and format the context block.

    Works with no workspace (global tier only). Returns (context, selected
    file count); context is "" when nothing is stored or the feature flag
    gates both tiers off.
    """
    tiers = _resolve_tiers(ws_path)
    if not tiers:
        return "", 0

    entries = _scan_tiers(tiers)
    selected = await _select_entries(query, entries, model_id=model_id)
    return _build_context(tiers, selected), len(selected)


async def _select_entries(
    query: str,
    entries: list[tuple[str, MemoryFile]],
    *,
    model_id: str,
) -> list[tuple[str, MemoryFile]]:
    """Pick up to MAX_SELECTIONS entries relevant to the query."""
    if not entries:
        return []

    # Few enough files: return all, no LLM needed
    if len(entries) <= MAX_SELECTIONS:
        return entries

    # On-device (local) turns must stay fully offline: skip the Haiku ranking
    # side-query and return the most recent memories. With a large store this
    # loses smart ranking, but it never leaves the machine — recall parity for
    # Gemma without breaking isolation.
    from server.local.runtime import is_local_model_id

    if is_local_model_id(model_id):
        return entries[:MAX_SELECTIONS]

    manifest = _build_tier_manifest(entries)

    try:
        selected_keys = await _query_selector(query, manifest, model_id)
    except Exception as e:
        log.warning("Memory recall side-query failed: %s", e)
        # Graceful degradation: return most recent files
        return entries[:MAX_SELECTIONS]

    key_to_entry = {_entry_key(scope, f): (scope, f) for scope, f in entries}
    result = []
    seen: set[str] = set()

    def _add(scope: str, f: MemoryFile) -> None:
        key = _entry_key(scope, f)
        if key not in seen:
            seen.add(key)
            result.append((scope, f))

    for raw_key in selected_keys[:MAX_SELECTIONS]:
        key = str(raw_key)
        if key in key_to_entry:
            _add(*key_to_entry[key])
            continue
        # Selector dropped or mangled the tier prefix. Filenames may be nested
        # (e.g. "aws/creds.md"), so strip one leading tier token if present and
        # match the remainder (or the raw key) against entry filenames,
        # newest first.
        head, _, tail = key.partition("/")
        stripped = tail if head in (SCOPE_GLOBAL, SCOPE_PROJECT) and tail else key
        for scope, f in entries:
            if f.filename in (stripped, key):
                _add(scope, f)
                break
    return result


async def _query_selector(query: str, manifest: str, model_id: str) -> list[str]:
    """Call Haiku to select relevant memory files."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from server.infrastructure.config import load_config

    config = load_config()
    region = config.get("bedrock_region", "us-east-1")
    chat_models = config.get("chat_models", {})
    # Use haiku for recall (cheapest model)
    haiku_model = chat_models.get("haiku", model_id)

    client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=BotoConfig(read_timeout=30, connect_timeout=5, retries={"max_attempts": 1}),
    )

    user_msg = (
        f"User query: {query}\n\n"
        f"Available memory files:\n{manifest}\n\n"
        "Select the most relevant files (up to 5). Respond with JSON only."
    )

    def _invoke():
        response = client.invoke_model(
            modelId=haiku_model,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": MAX_RECALL_TOKENS,
                    "system": RECALL_SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": user_msg}],
                }
            ),
        )
        body = json.loads(response["body"].read())
        text = body.get("content", [{}])[0].get("text", "")
        # Parse JSON response
        parsed = json.loads(text)
        return parsed.get("selected", [])

    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    return await loop.run_in_executor(executor, _invoke)


def _build_context(
    tiers: list[tuple[str, str]],
    selected: list[tuple[str, MemoryFile]],
) -> str:
    """Read selected memory files and format as a context block for prompt injection.

    Each tier's MEMORY.md index is always included (when present) so the model
    knows what else exists even if it was not selected.
    """
    parts = []
    for scope, memory_dir in tiers:
        index_content = load_memory_index(memory_dir)
        if index_content:
            parts.append(f"# Memory Index ({scope} MEMORY.md)\n{index_content}")

    for scope, mem_file in selected:
        path = mem_file.path
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue

        # Add freshness note
        try:
            mtime = os.path.getmtime(path)
            age_days = (
                datetime.now(tz=timezone.utc) - datetime.fromtimestamp(mtime, tz=timezone.utc)
            ).days
            if age_days == 0:
                freshness = "today"
            elif age_days == 1:
                freshness = "yesterday"
            else:
                freshness = f"{age_days} days ago"
        except OSError:
            freshness = "unknown"

        parts.append(
            f"# Memory: {mem_file.filename} (scope: {scope}, last updated: {freshness})\n{content}"
        )

    if not parts:
        return ""

    return "<memory-context>\n" + "\n\n".join(parts) + "\n</memory-context>"
