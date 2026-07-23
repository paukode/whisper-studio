"""Split a file's text into overlapping, line-anchored, structure-aware chunks.

Pure functions — no model, no IO — so they're cheap to unit-test. Chunks are
built on line boundaries (never mid-line) so each maps back to a real
``start_line``/``end_line`` range the UI and the model can cite.

Two refinements over a blind character window:
  - **token-budgeted**: sized by an estimated token count (~4 chars/token) so a
    chunk stays under the embedder's 512-token window and isn't truncated.
  - **structure-aware**: when the budget is hit, the chunk is cut at the nearest
    preceding *block boundary* (a markdown heading, a code definition, or a
    paragraph break) instead of mid-block — so a function or section tends to
    land whole in one chunk. A clean boundary cut starts the next chunk exactly
    at the boundary; only a forced mid-block cut carries an overlap.
"""

from __future__ import annotations

import re

from .config import CHUNK_MAX_TOKENS, CHUNK_OVERLAP_TOKENS

_HEADING_RE = re.compile(r"^#{1,6}\s")
_HEADING_TITLE_RE = re.compile(r"^(#{1,6})\s+(.*\S)")


def section_path(text: str, start_line: int) -> str:
    """The markdown heading breadcrumb in effect at ``start_line`` (1-based), e.g.
    ``"Overview > Q3 results"``. Includes the chunk's own heading when it starts on
    one, and its ancestor headings. Empty when no heading precedes the line.

    Used only to enrich a chunk's *embedding input* (so a section's meaning carries
    its document context); it never changes the stored chunk text or line anchors.
    Call only for heading-structured docs — a Python "# comment" would match too."""
    stack: dict[int, str] = {}
    for i, ln in enumerate(text.splitlines(), 1):
        if i > start_line:
            break
        m = _HEADING_TITLE_RE.match(ln)
        if m:
            level = len(m.group(1))
            for deeper in [k for k in stack if k > level]:
                del stack[deeper]
            stack[level] = m.group(2).strip()
    return " > ".join(stack[k] for k in sorted(stack))


# Lines that look like the start of a code definition/declaration. Deliberately
# broad and language-agnostic; a false positive in prose is harmless (it only
# ever serves as a *candidate* cut point used when the budget is already hit).
_CODE_DEF_RE = re.compile(
    r"^[ \t]{0,12}(?:@|def |async def |class |function |func |fn |"
    r"public |private |protected |internal |static |export |module |"
    r"interface |trait |impl |struct |enum |type \w|const \w+\s*=|"
    r"[A-Za-z_$][\w$]*\s*(?:=|:)?\s*(?:async\s+)?\(?[^=]*\)\s*(?:=>|\{))"
)


def _est_tokens(s: str) -> int:
    return max(1, len(s) // 4)


def _is_break(lines: list[str], i: int) -> bool:
    """True if line ``i`` is a good place to *start* a chunk (a block boundary)."""
    line = lines[i]
    if not line.strip():
        return False  # never start a chunk on a blank line
    if i > 0 and not lines[i - 1].strip():
        return True  # paragraph / block start (prev line blank)
    return bool(_HEADING_RE.match(line) or _CODE_DEF_RE.match(line))


def chunk_text(
    text: str, max_tokens: int = CHUNK_MAX_TOKENS, overlap_tokens: int = CHUNK_OVERLAP_TOKENS
) -> list[dict]:
    """Return ``[{start_line, end_line, text}]`` (1-based, inclusive lines)."""
    if not text or not text.strip():
        return []

    lines = text.splitlines()
    n = len(lines)
    chunks: list[dict] = []

    start = 0
    while start < n:
        size = 0
        end = start  # exclusive
        last_break = -1  # most recent block-boundary line within the window
        while end < n:
            tok = _est_tokens(lines[end]) + 1
            # Always take at least one line, even if it alone exceeds the budget.
            if size + tok > max_tokens and end > start:
                break
            if end > start and _is_break(lines, end):
                last_break = end
            size += tok
            end += 1

        # Prefer a structural cut (keep the trailing block whole for the next
        # chunk); fall back to the hard budget cut when there's no boundary.
        boundary_cut = end < n and last_break > start
        cut = last_break if boundary_cut else end

        body = "\n".join(lines[start:cut])
        if body.strip():
            chunks.append({"start_line": start + 1, "end_line": cut, "text": body})

        if cut >= n:
            break

        if boundary_cut:
            start = cut  # clean seam at the boundary — no overlap needed
        else:
            # Forced mid-block cut: rewind ~overlap_tokens for context overlap.
            back = 0
            ov = 0
            while back < (cut - start - 1) and ov < overlap_tokens:
                ov += _est_tokens(lines[cut - 1 - back]) + 1
                back += 1
            start = max(start + 1, cut - back)

    return chunks
