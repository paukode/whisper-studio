"""Memory extraction pipeline — post-query background agent.

After each chat turn (when stop_reason != tool_use), fires a background
agent to extract memories from the conversation. The agent is restricted
to memory tools + read-only workspace tools.

Runs with or without a workspace: with one, the agent sees both tiers and
routes writes by scope; without one, everything lands in global memory.
Cursor/throttle state is keyed by session id (a cursor is an index into one
session's message list, so keying it by workspace, as the old code did,
corrupted it whenever two sessions shared a workspace). It is always
persisted in the GLOBAL tier dir: a session can connect or disconnect a
workspace mid-life, so a workspace-dependent anchor would lose the cursor
on every flip.
"""

import json
import logging
import os

from server.memory.memdir import ensure_global_memory_dir, ensure_memory_dir
from server.memory.scan import build_manifest, scan_memory_files

log = logging.getLogger("whisper-studio")

# Throttle: extract every N chat turns
DEFAULT_EXTRACT_INTERVAL = 3

# Cursor + throttle state per session id (in-memory, persisted to .cursor.json
# so a server restart does not reset either: a lost cursor would re-extract old
# messages, and a lost turn counter would thin extraction for short sessions)
_cursors: dict[str, int] = {}  # session_id -> last processed message index
_turn_counters: dict[str, int] = {}  # session_id -> turns since last extraction
_inflight: set[str] = set()  # session_ids with an extraction agent running

# Cap on persisted per-session cursor entries (oldest dropped first)
_MAX_CURSOR_SESSIONS = 200
# Cap on in-memory per-session state (a long-lived server would otherwise
# accumulate two ints per session ever chatted in)
_MAX_STATE_SESSIONS = 1000


def drop_session(session_id: str) -> None:
    """Forget per-session extraction state. Wired to session deletion."""
    _cursors.pop(session_id, None)
    _turn_counters.pop(session_id, None)
    _inflight.discard(session_id)


def _trim_state() -> None:
    """Bound the in-memory dicts (oldest inserted dropped first)."""
    while len(_cursors) > _MAX_STATE_SESSIONS:
        _cursors.pop(next(iter(_cursors)))
    while len(_turn_counters) > _MAX_STATE_SESSIONS:
        _turn_counters.pop(next(iter(_turn_counters)))


def _cursor_path(memory_dir: str) -> str:
    return os.path.join(memory_dir, ".cursor.json")


def _load_state(memory_dir: str) -> dict:
    """Raw persisted state: {"sessions": {sid: cursor}, "turns": {sid: count}}."""
    try:
        with open(_cursor_path(memory_dir)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _load_cursor(memory_dir: str, session_id: str) -> int:
    """Load this session's cursor from disk. Returns 0 if not found.

    The legacy format ({"last_message_index": N}, keyed per workspace) is
    ignored on purpose: those indices pointed into whichever session last
    wrote them, so they are meaningless for any specific session.
    """
    try:
        return int(_load_state(memory_dir).get("sessions", {}).get(session_id, 0))
    except (AttributeError, TypeError, ValueError):
        return 0


def _load_turns(memory_dir: str, session_id: str) -> int:
    """Load this session's turns-since-last-extraction counter (0 if unknown)."""
    try:
        return int(_load_state(memory_dir).get("turns", {}).get(session_id, 0))
    except (AttributeError, TypeError, ValueError):
        return 0


def _save_state(
    memory_dir: str, session_id: str, *, cursor: int | None = None, turns: int | None = None
) -> None:
    """Persist this session's cursor and/or turn counter, keeping at most
    _MAX_CURSOR_SESSIONS entries per map. Maps not being written are carried
    through unchanged, so a cursor save never drops the turn counters."""
    path = _cursor_path(memory_dir)
    data = _load_state(memory_dir)
    out = {}
    for key, value in (("sessions", cursor), ("turns", turns)):
        entries = data.get(key)
        if not isinstance(entries, dict):
            entries = {}
        if value is not None:
            # Re-insert to refresh recency (dicts keep insertion order)
            entries.pop(session_id, None)
            entries[session_id] = value
            while len(entries) > _MAX_CURSOR_SESSIONS:
                entries.pop(next(iter(entries)))
        out[key] = entries
    try:
        with open(path, "w") as f:
            json.dump(out, f)
    except OSError as e:
        log.warning("Failed to save extraction state: %s", e)


def _has_memory_writes_in_messages(messages: list[dict], since_index: int) -> bool:
    """Check if any assistant message since cursor used memory_write."""
    for msg in messages[since_index:]:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    if block.get("name") == "memory_write":
                        return True
    return False


async def maybe_extract_memory(
    *,
    messages: list[dict],
    session_id: str,
    ws_path: str | None,
    model_id: str,
) -> None:
    """Post-query hook. Called as background task (fire-and-forget).

    Guards:
    1. Feature flag must be enabled (via ensure_*_memory_dir)
    2. Throttle: only every N turns
    3. In-flight: one extraction agent per session at a time
    4. Mutual exclusion: skip if model already wrote memory in this turn
    5. Cursor: only process new messages; claimed BEFORE the agent runs so
       overlapping turns cannot double-extract the same slice
    """
    try:
        # The global dir doubles as the cursor anchor; ensure_* helpers
        # return None when auto_memory is off, gating the whole pipeline.
        project_dir = ensure_memory_dir(ws_path)
        global_dir = ensure_global_memory_dir()
        if not global_dir:
            return

        # Throttle. The counter is persisted so short sessions keep
        # accumulating toward the every-N cadence across server restarts
        # instead of restarting from zero each boot.
        turns = _turn_counters.get(session_id)
        if turns is None:
            turns = _load_turns(global_dir, session_id)
        turns += 1
        _turn_counters[session_id] = turns if turns < DEFAULT_EXTRACT_INTERVAL else 0
        _save_state(global_dir, session_id, turns=_turn_counters[session_id])
        if turns < DEFAULT_EXTRACT_INTERVAL:
            _trim_state()
            return

        # One extraction agent per session at a time. The agent takes several
        # LLM turns; quick successive user turns must not stack a second run
        # over the same message slice.
        if session_id in _inflight:
            log.info("Skipping extraction — previous run still in flight")
            return

        # Load cursor
        cursor = _cursors.get(session_id)
        if cursor is None:
            cursor = _load_cursor(global_dir, session_id)
            _cursors[session_id] = cursor
        # A cursor beyond the list means it was persisted by a longer, stale
        # copy of this session (e.g. the session was truncated); reset.
        if cursor > len(messages):
            cursor = 0

        # Mutual exclusion: skip if model wrote memory in recent messages
        if _has_memory_writes_in_messages(messages, cursor):
            _cursors[session_id] = len(messages)
            _save_state(global_dir, session_id, cursor=len(messages))
            log.info("Skipping extraction — model already wrote memory")
            return

        # Get new messages since cursor
        new_messages = messages[cursor:]
        if not new_messages:
            return

        # Claim the slice up front: a failed run skips these messages rather
        # than risking duplicate extraction from a concurrent retry.
        _cursors[session_id] = len(messages)
        _save_state(global_dir, session_id, cursor=len(messages))
        _trim_state()

        _inflight.add(session_id)
        try:
            await _run_extraction(
                new_messages,
                global_dir=global_dir,
                project_dir=project_dir,
                model_id=model_id,
                session_id=session_id,
            )
        finally:
            _inflight.discard(session_id)

    except Exception as e:
        log.error("Memory extraction failed: %s", e, exc_info=True)


def _tier_manifest(global_dir: str | None, project_dir: str | None) -> str:
    """Existing-memory manifest with one section per live tier."""
    sections = []
    if global_dir:
        files = scan_memory_files(global_dir)
        sections.append(f"### Global (cross-workspace)\n{build_manifest(files)}")
    if project_dir:
        files = scan_memory_files(project_dir)
        sections.append(f"### Project (this workspace)\n{build_manifest(files)}")
    return "\n\n".join(sections) if sections else "(no memory files)"


async def _run_extraction(
    new_messages: list[dict],
    *,
    global_dir: str | None,
    project_dir: str | None,
    model_id: str,
    session_id: str,
) -> None:
    """Run extraction agent on new messages."""
    from server.agents.runtime import run_agent

    # Build transcript excerpt from new messages
    excerpt_parts = []
    for msg in new_messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_result":
                        text_parts.append(f"[tool result: {block.get('content', '')[:200]}]")
                elif isinstance(block, str):
                    text_parts.append(block)
            content = " ".join(text_parts)
        if content:
            excerpt_parts.append(f"{role}: {content[:500]}")

    excerpt = "\n".join(excerpt_parts[-20:])  # Last 20 messages, truncated

    # Pre-scan existing memories for context
    manifest = _tier_manifest(global_dir, project_dir)

    if project_dir:
        scope_note = (
            "Both memory tiers are available. Route each memory with the scope "
            "parameter: scope='global' for cross-project facts (user preferences, "
            "role, general feedback), scope='project' for facts tied to this "
            "workspace (goals, deadlines, repo-specific references)."
        )
    else:
        scope_note = (
            "No workspace is open, so only global memory is available. "
            "Save only cross-project facts (scope='global'); skip anything "
            "that is meaningless outside this conversation's missing project."
        )

    task = (
        f"Review the following conversation excerpt and extract important memories.\n\n"
        f"## Existing Memories\n{manifest}\n\n"
        f"## Recent Conversation\n{excerpt}\n\n"
        f"{scope_note} "
        f"Use memory_write to save new memories or update existing ones. "
        f"Use memory_read to check existing files before overwriting. "
        f"Be selective — only save what would be valuable in future sessions."
    )

    # Snapshot the stores before the agent runs: the event must report what
    # actually CHANGED on disk. tools_called counts attempted calls, so a
    # write blocked by the secret scanner (or failing on OSError) would
    # otherwise toast "1 memory saved" while the store is unchanged.
    before = _store_snapshot(global_dir, project_dir)

    result = await run_agent(
        task,
        agent_type="memory_extractor",
        session_id=session_id,
        depth=1,  # Prevent recursive extraction
    )

    if result.status == "completed":
        log.info(
            "Memory extraction completed: %d turns, tools: %s",
            result.turns_used,
            result.tools_called,
        )
        after = _store_snapshot(global_dir, project_dir)
        writes = sum(1 for k, sig in after.items() if before.get(k) != sig)
        deletes = sum(1 for k in before if k not in after)
        if writes or deletes:
            publish_memory_event(session_id, action="extracted", writes=writes, deletes=deletes)
    else:
        log.warning("Memory extraction %s: %s", result.status, result.output[:200])


def _store_snapshot(global_dir: str | None, project_dir: str | None) -> dict[str, tuple]:
    """(scope, filename) -> (mtime, size) for every topic file in the live tiers."""
    snapshot: dict[str, tuple] = {}
    for scope, d in (("global", global_dir), ("project", project_dir)):
        if not d:
            continue
        for m in scan_memory_files(d):
            snapshot[f"{scope}/{m.filename}"] = (m.mtime, m.size)
    return snapshot


def publish_memory_event(session_id: str, **payload) -> None:
    """Surface memory activity on the session's long-lived event stream.

    Extraction finishes after the chat SSE closed, so the per-turn stream
    cannot carry this; the /api/sessions/{id}/events channel (also used for
    cron events) outlives the turn. Best-effort: an event bus hiccup must
    never affect the extraction itself.
    """
    try:
        from server.agents.event_bus import event_bus

        event_bus.publish(session_id, {"type": "memory_event", "memoryEvent": payload})
    except Exception as e:
        log.debug("memory event publish skipped: %s", e)
