"""Markdown heading outline + section slicing for size-aware injection.

When a converted document is too large to inline whole, we send its heading
outline plus the lead section and let the model pull specific sections on demand
via the analyze_document tool. Sections are simple char-offset slices keyed by a
1-based number that matches the outline.
"""

import re

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def build_outline(text: str):
    """Return ``(outline_markdown, sections)``.

    ``sections`` is a list of dicts ``{num, level, title, start, end}`` of char
    offsets into ``text``. Both are empty when the document has no Markdown
    headings (e.g. plain text or a flat spreadsheet dump).
    """
    heads = list(_HEADING.finditer(text or ""))
    if not heads:
        return "", []

    sections = []
    for i, m in enumerate(heads):
        start = m.start()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        sections.append(
            {
                "num": str(i + 1),
                "level": len(m.group(1)),
                "title": m.group(2).strip(),
                "start": start,
                "end": end,
            }
        )

    lines = [f"{'  ' * (s['level'] - 1)}{s['num']}. {s['title']}" for s in sections]
    return "\n".join(lines), sections


def get_section(text: str, sections, num) -> str | None:
    """Return the slice of ``text`` for section ``num`` (str or int), or None."""
    target = str(num).strip()
    for s in sections or []:
        if s["num"] == target:
            return text[s["start"] : s["end"]]
    return None
