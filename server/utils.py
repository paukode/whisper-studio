"""
Shared utilities for Whisper Studio.
"""

from collections import deque

# ── BoundedUUIDSet ─────────────────────────────────────────────────────────────
# O(1) ring-buffer dedup set.
# Keeps the last `capacity` seen UUIDs; evicts the oldest on overflow.


class BoundedUUIDSet:
    def __init__(self, capacity: int = 256):
        self._buf: deque = deque(maxlen=capacity)
        self._set: set = set()

    def add(self, uid: str):
        if len(self._buf) == self._buf.maxlen:
            # Evict oldest before appending (deque does it automatically, but we
            # must sync the set manually)
            self._set.discard(self._buf[0])
        self._buf.append(uid)
        self._set.add(uid)

    def has(self, uid: str) -> bool:
        return uid in self._set

    def clear(self):
        self._buf.clear()
        self._set.clear()


# ── NDJSON-safe JSON serialisation ─────────────────────────────────────────────
# JSON.stringify allows U+2028 / U+2029 raw in strings, but NDJSON receivers
# split on any Unicode line terminator — escape them to keep lines intact.
# Escapes Unicode line terminators so NDJSON receivers don't split on them.

import json as _json  # noqa: E402


def ndjson_dumps(value) -> str:
    s = _json.dumps(value)
    return s.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
