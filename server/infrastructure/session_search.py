"""Full-text search over past chat sessions (SQLite FTS5).

Backs the model-callable ``session_search`` tool and the sidebar's content
search. Every session's ``chat_history`` is a JSON blob on the sessions row;
this module mirrors each session into one FTS5 table and keeps the mirror in
step on every save, append, and delete. Rows carry a role so each consumer
picks what it searches:

    user / assistant   the prompt-role message text (msg_index = position)
    tool               tool_result payloads inside those messages (same index)
    transcript         voice transcript segments (msg_index from TRANSCRIPT_BASE)
    title              the session title (msg_index -1)

The sidebar searches user, assistant and transcript text (never tool
payloads: users do not read those as messages). The model's tool searches
user, assistant, title and transcript by default and can opt into tool
output.

No LLM calls anywhere: search returns the actual stored messages. That is
what makes compaction recoverable: the request-side history gets summarized,
the persisted history does not, and ``session_search`` can pull any
summarized-away message back verbatim.

FTS5 is compiled into every Python this app ships with (verified in the venv
and the standalone macOS build); should it be missing, ``available()`` is
False and callers fall back to their pre-FTS behaviour.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3

log = logging.getLogger("whisper-studio")

FTS_TABLE = "session_messages_fts"
PROMPT_ROLES = frozenset({"user", "assistant"})
SIDEBAR_ROLES: tuple[str, ...] = ("user", "assistant", "transcript")
MODEL_ROLES: tuple[str, ...] = ("user", "assistant", "title", "transcript")
TRANSCRIPT_BASE = 100_000  # transcript segment i is indexed at TRANSCRIPT_BASE + i
TITLE_INDEX = -1
MAX_TEXT_CHARS = 1500  # per returned message
MAX_INDEXED_CHARS = 20_000  # per indexed row
MAX_SNIPPET_CHARS = 140
DEFAULT_WINDOW = 5
DEFAULT_LIMIT = 5

_MARK_L = "\x02"
_MARK_R = "\x03"
_fts_ok: bool | None = None


def _split_content(content) -> tuple[str, str]:
    """(message text, tool-result text) for one message's content."""
    if isinstance(content, str):
        return content, ""
    if not isinstance(content, list):
        return "", ""
    text: list[str] = []
    tool: list[str] = []
    for b in content:
        if isinstance(b, dict):
            t = b.get("type")
            if t == "text" and b.get("text"):
                text.append(str(b["text"]))
            elif t == "tool_result":
                inner = b.get("content", "")
                if isinstance(inner, list):
                    inner = " ".join(
                        str(x.get("text", ""))
                        for x in inner
                        if isinstance(x, dict) and x.get("text")
                    )
                if inner:
                    tool.append(str(inner))
        elif isinstance(b, str):
            text.append(b)
    return "\n".join(text), "\n".join(tool)


def _flatten(content) -> str:
    """Readable text of one message (tool payloads marked) for read views."""
    text, tool = _split_content(content)
    parts = [text] if text else []
    if tool:
        parts.append(f"[tool result] {tool}")
    return "\n".join(parts)


def ensure_fts(conn: sqlite3.Connection) -> bool:
    """Create the FTS5 mirror table if needed. False when FTS5 is unavailable."""
    global _fts_ok
    try:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5("
            "session_id UNINDEXED, msg_index UNINDEXED, role UNINDEXED, text, "
            "tokenize='porter unicode61')"
        )
        _fts_ok = True
    except sqlite3.OperationalError as e:
        if _fts_ok is not False:
            log.warning("session search: FTS5 unavailable (%s); falling back to scans", e)
        _fts_ok = False
    return _fts_ok


def available(conn: sqlite3.Connection | None = None) -> bool:
    if _fts_ok is not None and conn is None:
        return _fts_ok
    if conn is None:
        from server.infrastructure.sessions import _get_conn

        with _get_conn() as c:
            return ensure_fts(c)
    return ensure_fts(conn)


def _segment_texts(segments) -> list[str]:
    if isinstance(segments, str):
        try:
            segments = json.loads(segments) or []
        except (TypeError, ValueError):
            segments = []
    out: list[str] = []
    for seg in segments or []:
        text = seg.get("text") if isinstance(seg, dict) else None
        out.append(text.strip() if isinstance(text, str) else "")
    return out


def reindex_session(
    conn: sqlite3.Connection,
    session_id: str,
    history: list | None,
    *,
    segments=None,
    title: str | None = None,
) -> None:
    """Replace the mirror rows of one session. ``segments`` and ``title``
    default to the values on the sessions row (the append path only has the
    history in hand). Best-effort: never raises into the caller's save."""
    try:
        if not ensure_fts(conn):
            return
        if segments is None or title is None:
            row = conn.execute(
                "SELECT title, segments FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is not None:
                if title is None:
                    title = row["title"]
                if segments is None:
                    segments = row["segments"]
        conn.execute(f"DELETE FROM {FTS_TABLE} WHERE session_id = ?", (session_id,))
        rows: list[tuple] = []
        if title and str(title).strip():
            rows.append((session_id, TITLE_INDEX, "title", str(title).strip()[:MAX_INDEXED_CHARS]))
        for i, msg in enumerate(history or []):
            if not isinstance(msg, dict) or msg.get("role") not in PROMPT_ROLES:
                continue
            text, tool = _split_content(msg.get("content"))
            if text.strip():
                rows.append((session_id, i, msg["role"], text.strip()[:MAX_INDEXED_CHARS]))
            if tool.strip():
                rows.append((session_id, i, "tool", tool.strip()[:MAX_INDEXED_CHARS]))
        for i, text in enumerate(_segment_texts(segments)):
            if text:
                rows.append(
                    (session_id, TRANSCRIPT_BASE + i, "transcript", text[:MAX_INDEXED_CHARS])
                )
        if rows:
            conn.executemany(
                f"INSERT INTO {FTS_TABLE}(session_id, msg_index, role, text) VALUES (?, ?, ?, ?)",
                rows,
            )
    except sqlite3.Error as e:
        log.debug("session search reindex skipped for %s: %s", session_id, e)


def drop_session(conn: sqlite3.Connection, session_id: str) -> None:
    try:
        if ensure_fts(conn):
            conn.execute(f"DELETE FROM {FTS_TABLE} WHERE session_id = ?", (session_id,))
    except sqlite3.Error as e:
        log.debug("session search drop skipped for %s: %s", session_id, e)


def rebuild_all(conn: sqlite3.Connection) -> int:
    """Re-mirror every session. Returns the number of sessions indexed."""
    if not ensure_fts(conn):
        return 0
    rows = conn.execute("SELECT id, title, segments, chat_history FROM sessions").fetchall()
    n = 0
    for row in rows:
        try:
            history = json.loads(row["chat_history"]) or []
        except (TypeError, ValueError):
            history = []
        reindex_session(conn, row["id"], history, segments=row["segments"], title=row["title"])
        n += 1
    return n


_TOKEN_RE = re.compile(r"[\w\-]+", re.UNICODE)


def _safe_match(query: str, *, prefix: bool = False) -> str:
    """A tolerant FTS5 MATCH expression: each token quoted (so punctuation and
    reserved words cannot break the parser), implicitly ANDed. ``prefix``
    matches partial words too (the sidebar's as-you-type search)."""
    tokens = [t for t in _TOKEN_RE.findall(query or "") if t]
    if not tokens:
        return ""
    star = "*" if prefix else ""
    return " ".join('"' + t.replace('"', '""') + '"' + star for t in tokens[:12])


def _window_snippet(snip: str) -> str:
    """Plain-text snippet under MAX_SNIPPET_CHARS, centred on the first match.
    FTS5's own windowing is token-based, so one huge token comes back whole."""
    start = snip.find(_MARK_L)
    end = snip.find(_MARK_R, start + 1) if start >= 0 else -1
    plain = snip.replace(_MARK_L, "").replace(_MARK_R, "")
    if len(plain) <= MAX_SNIPPET_CHARS:
        return plain
    if start < 0:
        return plain[: MAX_SNIPPET_CHARS - 1] + "…"
    # Positions in the marker-free text.
    lo_match = start
    hi_match = max(lo_match, end - 1)
    half = (MAX_SNIPPET_CHARS - (hi_match - lo_match)) // 2
    lo = max(0, lo_match - half)
    hi = min(len(plain), hi_match + half)
    out = plain[lo:hi]
    if lo > 0:
        out = "…" + out
    if hi < len(plain):
        out = out + "…"
    return out


def _run_match(conn: sqlite3.Connection, expr: str, roles: tuple[str, ...], limit: int):
    role_sql = ",".join("?" for _ in roles)
    sql = (
        f"SELECT session_id, msg_index, role, "
        f"snippet({FTS_TABLE}, 3, ?, ?, ' … ', 24) AS snip, bm25({FTS_TABLE}) AS rank "
        f"FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ? AND role IN ({role_sql}) "
        f"ORDER BY rank LIMIT ?"
    )
    return conn.execute(sql, (_MARK_L, _MARK_R, expr, *roles, limit)).fetchall()


def search(
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    roles: tuple[str, ...] = MODEL_ROLES,
    session_id: str | None = None,
    exclude_session: str | None = None,
    prefix: bool = False,
) -> list[dict]:
    """Discovery: best hit per session, ranked. Each result carries the
    session id and title, the matched message index and role, and a short
    plain-text snippet. Empty when FTS5 is unavailable (callers fall back).
    ``prefix`` skips the raw-syntax attempt and matches partial words."""
    from server.infrastructure.sessions import _get_conn

    limit = max(1, min(int(limit or DEFAULT_LIMIT), 50))
    roles = tuple(roles) or MODEL_ROLES
    with _get_conn() as conn:
        if not ensure_fts(conn):
            return []
        raw = (query or "").strip()
        rows = []
        if raw and not prefix:
            # The model may hand us real FTS5 syntax (phrases, OR, NOT, prefix*).
            try:
                rows = _run_match(conn, raw, roles, limit * 8)
            except sqlite3.OperationalError:
                rows = []
        if raw and not rows:
            # Tolerant fallback: every token quoted, ANDed (prefix-matched for
            # the sidebar's as-you-type search).
            expr = _safe_match(raw, prefix=prefix)
            if expr:
                try:
                    rows = _run_match(conn, expr, roles, limit * 8)
                except sqlite3.OperationalError as e:
                    log.debug("session search query failed: %s", e)
                    rows = []
        best: dict[str, dict] = {}
        for r in rows:
            sid = r["session_id"]
            if exclude_session and sid == exclude_session:
                continue
            if session_id and sid != session_id:
                continue
            if sid in best:
                continue
            best[sid] = {
                "session_id": sid,
                "msg_index": int(r["msg_index"]),
                "role": r["role"],
                "snippet": _window_snippet(r["snip"] or ""),
            }
            if len(best) >= limit:
                break
        if not best:
            return []
        ids = list(best)
        marks = ",".join("?" for _ in ids)
        meta = conn.execute(
            f"SELECT id, title, updated_at FROM sessions WHERE id IN ({marks})", ids
        ).fetchall()
    titles = {m["id"]: (m["title"], m["updated_at"]) for m in meta}
    out = []
    for sid, hit in best.items():
        title, when = titles.get(sid, ("(deleted)", ""))
        out.append({**hit, "title": title, "updated_at": when})
    return out


def read_window(
    session_id: str, *, around_index: int | None = None, window: int = DEFAULT_WINDOW
) -> dict | None:
    """Scroll or read: the prompt-role messages of one session around an
    index (or its head and tail when no index is given)."""
    from server.infrastructure.sessions import _get_conn

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT title, updated_at, chat_history FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    if not row:
        return None
    try:
        history = json.loads(row["chat_history"]) or []
    except (TypeError, ValueError):
        history = []
    visible = [
        {"index": i, "role": m.get("role"), "text": _flatten(m.get("content")).strip()}
        for i, m in enumerate(history)
        if isinstance(m, dict) and m.get("role") in PROMPT_ROLES
    ]
    visible = [v for v in visible if v["text"]]
    for v in visible:
        if len(v["text"]) > MAX_TEXT_CHARS:
            v["text"] = v["text"][:MAX_TEXT_CHARS].rstrip() + " ..."
    window = max(1, min(int(window or DEFAULT_WINDOW), 25))
    total = len(visible)
    if around_index is None:
        if total <= 2 * window:
            slice_ = visible
        else:
            elided = {
                "index": -1,
                "role": "elided",
                "text": f"... {total - 2 * window} messages elided ...",
            }
            slice_ = visible[:window] + [elided] + visible[-window:]
        before = after = 0
    else:
        pos = next((k for k, v in enumerate(visible) if v["index"] >= around_index), total - 1)
        pos = max(0, pos)
        lo = max(0, pos - window)
        hi = min(total, pos + window + 1)
        slice_ = visible[lo:hi]
        before, after = lo, total - hi
    return {
        "session_id": session_id,
        "title": row["title"],
        "updated_at": row["updated_at"],
        "message_count": total,
        "messages_before": before,
        "messages_after": after,
        "messages": slice_,
    }


def browse(*, limit: int = 10, exclude_session: str | None = None) -> list[dict]:
    """Recent sessions, newest first, with a one-line preview."""
    from server.infrastructure.sessions import CRON_INBOX_ID, _get_conn

    limit = max(1, min(int(limit or 10), 50))
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, updated_at, chat_history FROM sessions "
            "WHERE archived = 0 ORDER BY updated_at DESC LIMIT ?",
            (limit + 2,),
        ).fetchall()
    out = []
    for r in rows:
        if r["id"] in (exclude_session, CRON_INBOX_ID):
            continue
        try:
            history = json.loads(r["chat_history"]) or []
        except (TypeError, ValueError):
            history = []
        first = next(
            (
                _flatten(m.get("content")).strip()
                for m in history
                if isinstance(m, dict) and m.get("role") == "user"
            ),
            "",
        )
        out.append(
            {
                "session_id": r["id"],
                "title": r["title"],
                "updated_at": r["updated_at"],
                "message_count": sum(
                    1 for m in history if isinstance(m, dict) and m.get("role") in PROMPT_ROLES
                ),
                "preview": first[:160],
            }
        )
        if len(out) >= limit:
            break
    return out


__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_WINDOW",
    "FTS_TABLE",
    "MODEL_ROLES",
    "SIDEBAR_ROLES",
    "TITLE_INDEX",
    "TRANSCRIPT_BASE",
    "available",
    "browse",
    "drop_session",
    "ensure_fts",
    "read_window",
    "rebuild_all",
    "reindex_session",
    "search",
]
