"""The workspace-file citation contract, in one place.

A citation is a markdown link whose fragment the chat UI intercepts:

    [<display path>:<start>-<end>](#wsfile=<url-quoted href path>&L=<start>-<end>)

The ``#wsfile=`` fragment survives HTML sanitization (that is why a fragment
scheme was chosen). ``quote`` percent-encodes any literal ``&``/``:`` in the path,
so the FIRST raw ``&`` in the fragment is unambiguously the path/params boundary —
the client splits there before decoding. The optional ``&L=`` param is additive:
links without it (old transcripts) still open the file, just at the top.

Both emitters (the ``workspace_semantic_search`` tool and the grounding block) use
this so display text stays human-readable (relative) while the href is canonical
(absolute), which also means the same file cited from either place opens one dock
panel instead of two.
"""

from __future__ import annotations

from urllib.parse import quote


def citation_link(display_path: str, start: int, end: int, href_path: str) -> str:
    """Build the canonical ``#wsfile`` citation link. ``display_path`` is what the
    reader sees (kept relative); ``href_path`` is the target the client opens
    (absolute, so it resolves regardless of which workspace is connected)."""
    return f"[{display_path}:{start}-{end}](#wsfile={quote(href_path)}&L={start}-{end})"
