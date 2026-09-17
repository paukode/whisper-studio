"""Resolve a folder the user named to a real directory.

People say and type folder names loosely: "the ml-ops folder in Documents",
"ML-OPS", or, through speech recognition, "m l dash o p s". ``resolve_folder``
turns such a reference into one existing directory when the match is
unambiguous, or into a short list of candidates for the user to confirm.

Search space (immediate children only, so it stays cheap): the parent of the
given path when it exists, the usual home roots, and the parents of recent
workspaces. Matching is case-insensitive and ignores punctuation, so
``ml-ops`` == ``ML_OPS`` == ``ml ops``; spoken punctuation words ("dash",
"underscore", "dot") are folded first.
"""

from __future__ import annotations

import difflib
import os
import re
from collections.abc import Iterable

DEFAULT_ROOTS: tuple[str, ...] = (
    "~",
    "~/Documents",
    "~/Desktop",
    "~/Downloads",
    "~/Developer",
    "~/Projects",
    "~/Code",
    "~/src",
    "~/repos",
    "~/work",
)

# Spoken punctuation, as speech recognition writes it.
_SPOKEN = (
    (r"\b(?:dash|hyphen|minus)\b", "-"),
    (r"\bunderscore\b", "_"),
    (r"\b(?:dot|point)\b", "."),
    (r"\bslash\b", "/"),
)

_MAX_SEARCH_DIRS = 14
_MAX_CANDIDATES = 6
_CLOSE_RATIO = 0.72
_SURE_RATIO = 0.9
# Words too generic to make two folder names related on their own.
_GENERIC_TOKENS = frozenset(
    {"the", "and", "new", "old", "test", "tests", "data", "src", "app", "main", "project", "repo"}
)


def _tokens(name: str) -> set[str]:
    """Distinctive word parts of a folder name: "orders-etl" -> {orders, etl}."""
    parts = re.split(r"[^a-z0-9]+", spoken_to_name(name).lower())
    return {p for p in parts if len(p) >= 3 and p not in _GENERIC_TOKENS}


def browse_names(
    query: str,
    *,
    roots: Iterable[str] | None = None,
    recents: Iterable[str] | None = None,
    limit: int = 40,
) -> list[str]:
    """The folders that actually exist where ``query`` was looked for (the
    named root first), so a caller can offer REAL names instead of guessing.
    Names, not paths; hidden folders skipped; capped at ``limit``."""
    names: list[str] = []
    seen: set[str] = set()
    for directory in _search_dirs(query, roots or DEFAULT_ROOTS, list(recents or [])):
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name.lower())
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
            except OSError:
                continue
            if entry.name not in seen:
                seen.add(entry.name)
                names.append(entry.name)
            if len(names) >= limit:
                return names
    return names


def spoken_to_name(text: str) -> str:
    """Fold spoken punctuation into characters: "m l dash o p s" -> "m l-o p s"."""
    out = text.strip()
    for pattern, repl in _SPOKEN:
        out = re.sub(rf"\s*{pattern}\s*", repl, out, flags=re.IGNORECASE)
    return out


def normalize(name: str) -> str:
    """Comparison key: lowercase alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", spoken_to_name(name).lower())


def _root_word(text: str) -> str | None:
    """A root the user named ("... in my Documents") narrows the search."""
    lowered = text.lower()
    for word, root in (
        ("documents", "~/Documents"),
        ("desktop", "~/Desktop"),
        ("downloads", "~/Downloads"),
        ("developer", "~/Developer"),
        ("projects", "~/Projects"),
    ):
        if re.search(rf"\b{word}\b", lowered):
            return root
    return None


def _search_dirs(query: str, roots: Iterable[str], recents: Iterable[str]) -> list[str]:
    dirs: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        real = os.path.realpath(os.path.expanduser(path))
        if real not in seen and os.path.isdir(real):
            seen.add(real)
            dirs.append(real)

    expanded = os.path.expanduser(query)
    parent = os.path.dirname(expanded.rstrip("/"))
    if parent and os.path.isabs(parent):
        add(parent)
    root_list = [os.path.expanduser(r) for r in roots]
    named_root = _root_word(query)
    if named_root:
        # "... in my Documents": search that root first, but only when it is one
        # of the configured roots (keeps tests and custom setups self-contained).
        base = os.path.basename(named_root).lower()
        for r in root_list:
            if os.path.basename(r.rstrip("/")).lower() == base:
                add(r)
    for root in root_list:
        add(root)
    for rec in recents:
        add(os.path.dirname(rec.rstrip("/")))
    return dirs[:_MAX_SEARCH_DIRS]


def _target_name(query: str) -> str:
    """The folder name to look for: the last path segment, minus filler words
    that describe where it lives ("folder", "in my documents")."""
    text = spoken_to_name(query)
    segment = text.rstrip("/").split("/")[-1] if "/" in text else text
    segment = re.sub(
        r"\b(?:the|a|my|folder|directory|repo|repository|project|in|inside|from|under|of|documents|desktop|downloads|developer|projects|called|named)\b",
        " ",
        segment,
        flags=re.IGNORECASE,
    )
    return segment.strip()


def resolve_folder(
    query: str,
    *,
    roots: Iterable[str] | None = None,
    recents: Iterable[str] | None = None,
) -> tuple[str | None, list[str]]:
    """Return ``(path, [])`` for one confident match, ``(None, candidates)`` when
    the user must choose (candidates may be empty when nothing is close)."""
    query = (query or "").strip()
    if not query:
        return None, []
    expanded = os.path.expanduser(spoken_to_name(query) if "/" in query or "~" in query else query)
    if os.path.isdir(expanded):
        return os.path.realpath(expanded), []

    target = normalize(_target_name(query))
    if not target:
        return None, []
    target_tokens = _tokens(_target_name(query))
    recents = list(recents or [])
    for rec in recents:
        if normalize(os.path.basename(rec.rstrip("/"))) == target and os.path.isdir(rec):
            return os.path.realpath(rec), []

    exact: list[str] = []
    close: list[tuple[float, str]] = []
    for directory in _search_dirs(query, roots or DEFAULT_ROOTS, recents):
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
            except OSError:
                continue
            key = normalize(entry.name)
            if not key:
                continue
            if key == target:
                exact.append(entry.path)
                continue
            ratio = difflib.SequenceMatcher(None, key, target).ratio()
            # "ml" should offer ml-ops and ml-reports: a short prefix counts.
            contained = (len(target) >= 3 and (target in key or key in target)) or (
                len(target) >= 2 and key.startswith(target)
            )
            # Speech recognition mangles one word and keeps the other:
            # "flight-etl" for orders-etl still shares "etl", so every
            # *-etl folder is a candidate worth confirming.
            shared = bool(target_tokens & _tokens(entry.name))
            if ratio >= _CLOSE_RATIO or contained or shared:
                score = max(ratio, 0.8 if contained else 0.0, 0.6 if shared else 0.0)
                close.append((score, entry.path))

    if len(exact) == 1:
        return os.path.realpath(exact[0]), []
    if exact:
        return None, [os.path.realpath(p) for p in exact[:_MAX_CANDIDATES]]
    close.sort(key=lambda item: item[0], reverse=True)
    candidates: list[str] = []
    for _ratio, path in close:
        real = os.path.realpath(path)
        if real not in candidates:
            candidates.append(real)
    candidates = candidates[:_MAX_CANDIDATES]
    if len(candidates) == 1 and close[0][0] >= _SURE_RATIO:
        return candidates[0], []
    return None, candidates
