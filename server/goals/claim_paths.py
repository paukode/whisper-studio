"""The files, folders and documents a clause claims (read for
server/goals/claims.py): where a path is named (a markdown link, backticks,
quotes, a table cell, bare), and whether the clause says the file was made,
changed, removed or is somewhere.

Pure functions over text; no I/O.
"""

from __future__ import annotations

import re
from typing import NamedTuple
from urllib.parse import unquote

from server.goals.claim_text import (
    _APOS,
    _BY_OTHER_RE,
    _DATED_RE,
    _GONE_VERBS,
    _LEAD_MADE_RE,
    _LIST_ITEM_RE,
    _MADE_VERBS,
    _MASK,
    _NOT_MADE_RE,
    _OFFER_AHEAD_RE,
    _STATE_RE,
    _URL_RE,
    _VERB_RE,
    FILE,
    FOLDER,
    OTHERS_WORK_RE,
    REMOVED,
    Claim,
    _is_question,
    _subject,
)

# Extensions a reply plausibly hands the user as a finished artifact. Code and
# config files are deliberately absent: a reply mentioning `/src/app.py` is
# describing work, not claiming a deliverable.
_DELIVERABLE_EXTS = "docx|xlsx|pptx|pdf|html?|md|csv|json|txt|png|svg|zip|dmg"
_EXT = rf"\.(?:{_DELIVERABLE_EXTS})"
_EXT_RE = re.compile(rf"{_EXT}\b", re.IGNORECASE)

# The app's own file link: [label](#wsfile=/abs/or/relative/path&open=os). Its
# builder url-quotes the path (server/index/citations.py), so a space arrives
# as %20 and is decoded, as the chat's own link handler decodes it.
_WSFILE = "#wsfile="
_WSFILE_RE = re.compile(r"#wsfile=([^&)\s\"']+)(?![^&)\s\"']|&L=)")
# An absolute or home-relative path with a deliverable extension, as it
# appears in prose or backticks. The lookbehind keeps it from matching the
# tail of a longer token, and a path never starts "//", which is the rest of a
# web address ("https://host/a.pdf" names no file on disk); the trailing \b
# keeps "report.docx." clean. A file:// address names the path after its
# scheme.
_ABS_PATH_RE = re.compile(rf"(?<![\w/.])((?:~|/(?!/))[^\s`'\"()\[\]<>]*?{_EXT})\b", re.IGNORECASE)
_FILE_URL_RE = re.compile(rf"\bfile://((?:~|/)[^\s`'\"()\[\]<>]*?{_EXT})\b", re.IGNORECASE)
# Where the reply marks both ends of a path, the path may hold spaces, as Mac
# file names often do ("~/Downloads/Q3 Report.docx"): inside backticks or
# double quotes, as a markdown link's target (bare, <angle-bracketed>, or
# url-encoded, which is decoded), or as a whole table cell. A marked run that
# holds a second path or a path and more words ("~/a.md and ~/b.md") is not
# one path, and the bare paths inside it are read instead.
_BACKTICK_PATH_RE = re.compile(rf"`((?:~|/)[^`\n]*?{_EXT})`", re.IGNORECASE)
_QUOTED_PATH_RE = re.compile(
    rf"[\"\u201c]((?:~|/)[^\"\u201c\u201d\n]*?{_EXT})[\"\u201d]", re.IGNORECASE
)
_LINK_RE = re.compile(r"\[([^\]\n]*)\]\(\s*(?:<([^<>\n]+)>|([^()<>\n]+?))\s*\)")
_CELL_PATH_RE = re.compile(rf"\s*[*_]*((?:~|/)[^|`\"<>\n]*?{_EXT})[*_]*\s*", re.IGNORECASE)
_WHOLE_PATH_RE = re.compile(rf"(?:~|/).*{_EXT}", re.IGNORECASE)
_TWO_PATHS_RE = re.compile(rf"\s(?:~|/)|{_EXT}\s", re.IGNORECASE)

# ── Reading a reply for claims ─────────────────────────────────────────────
# A path is a claim only where the reply says the file was made. Its clause
# has a completion word ("saved", "created", "is ready", "here"), or puts the
# path right after a location word ("to", "at", "in", "as", a colon); the
# app's own file link is a claim by itself. A clause that negates, speaks of
# what would happen, gives an example, or offers or plans in the first person
# ahead of the path is not a claim, and neither is a question or a code
# block. So "Saved to X, but the logo was not included" claims X, while "Want
# me to save it as X?", "I could not write X", "I will write X next", "2.
# write X" and "for example X" claim nothing, and a correction that names the
# missing path settles the check instead of repeating it. A list item is read
# with the line that introduces it ("I created these files:"). The words of a
# file name are not the reply's words ("~/Not Final.docx" negates nothing),
# and a comma or dash inside a marked path does not split its clause.
#
# A markdown table whose header names a file, path, location, output or saved
# column lists deliverables: each of its rows is read whole, with the table's
# lead-in line, and every path in a row is claimed unless the row negates,
# offers or asks, or says its file is gone ("| old.md | deleted |" in a table
# of changes). The rows of any other table read as prose, as before.

_TABLE_RULE_RE = re.compile(r"\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*")
_FILE_COLUMN_RE = re.compile(
    r"\b(?:file(?:name|path)?s?|paths?|locations?|outputs?|saved)\b", re.IGNORECASE
)
_GONE_RE = re.compile(r"\b(?:deleted|removed|trashed|moved|renamed)\b", re.IGNORECASE)

_DONE_RE = re.compile(
    r"\b(?:saved|wrote|written|created|exported|generated|produced|stored|placed|put"
    r"|rendered|built|converted|updated|downloaded|attached|ready|available|here|find"
    r"|located|lives)\b|\b(?:is|are) (?:now )?(?:at|in)\b",
    re.IGNORECASE,
)
_LOCATED_RE = re.compile(r"(?:\b(?:to|at|in|as|into|under)|:)[\s`*_\"'(\[]*$", re.IGNORECASE)
_LINK_OPEN_RE = re.compile(r"\[[^\]]*\]\(\s*$")

_PATHS_ONLY_RE = re.compile(r"[^A-Za-z]+|\band\b|\bor\b", re.IGNORECASE)

# ── Beyond documents ───────────────────────────────────────────────────────
# Any other file: under a local root or the home folder, or relative with a
# folder in it ("server/app.py", which reads against the workspace), with an
# extension; a hidden file or folder ("~/.zshrc", "~/.ssh/config"). A folder
# is a home or rooted path whose last part has no extension ("~/Downloads",
# "/Users/me/Projects/app/"). An absolute path counts only under a local root:
# "/api/v1/users" is a route, not a place on disk.

_ROOT = r"(?:~|/(?:Users|Volumes|private|tmp|var|opt|Applications|Library|home|mnt|srv)(?=/|\b))"
_SEGMENT = r"\.?[\w@+-][\w.@+-]*"
_OTHER_FILE_RE = re.compile(
    r"(?<![\w/.~@:%-])("
    rf"{_ROOT}/(?:{_SEGMENT}/)*{_SEGMENT}\.[A-Za-z0-9]{{1,10}}"
    rf"|(?:\./)?(?:[\w@+-][\w.@+-]*/)+{_SEGMENT}\.[A-Za-z0-9]{{1,10}}"
    r"|~/\.[\w.@+-]+"
    r")(?![\w/])"
)
_FOLDER_RE = re.compile(
    rf"(?<![\w/.~@:%-])({_ROOT}(?:/\.?[\w@+-][\w@+ .-]*?)+/?)(?=[\s`'\".,;:)!?*_]|$)"
)
_FILE_NAME_RE = re.compile(r"(?<!^)(?<!/)\.[A-Za-z0-9]{1,10}$")

# ── Finding the paths a text names ─────────────────────────────────────────


class _Span(NamedTuple):
    """One path a text names: where its mention starts and ends (a link's
    "[", an opening quote, or the path itself), the path, whether it is the
    app's own file link, and where the part that is the path's own text
    begins (a link's prose label, "here", stays readable)."""

    start: int
    end: int
    path: str
    app_link: bool
    mask_from: int


def _free(spans: list[_Span], start: int, end: int) -> bool:
    return all(end <= s.start or start >= s.end for s in spans)


def _link_path(target: str) -> tuple[str, bool] | None:
    """The file a markdown link's target names, decoded, and whether it is the
    app's own file link; None for any other target (a web page, an anchor, a
    relative name). The app's link splits at its first raw "&" before
    decoding, as the chat's handler does."""
    target = target.strip()
    if target.startswith(_WSFILE):
        raw = target[len(_WSFILE) :]
        if "&L=" in raw:
            # A search result's citation points at a source; it made nothing.
            return None
        path = unquote(raw.split("&", 1)[0]).strip()
        return (path, True) if path else None
    if target[:7].lower() == "file://":
        target = target[7:]
    path = unquote(target)
    if _WHOLE_PATH_RE.fullmatch(path) and not _TWO_PATHS_RE.search(path):
        return path, False
    return None


def _path_spans(text: str) -> list[_Span]:
    """Every path ``text`` names as a deliverable, in text order: a marked
    path first (a link target, backticks, double quotes), then a file://
    address, the app's link and a bare path outside those."""
    spans: list[_Span] = []
    for m in _LINK_RE.finditer(text):
        found = _link_path(m.group(2) or m.group(3) or "")
        if found and _free(spans, *m.span()):
            # A label that names a file is part of the name; "here" is prose.
            label_is_name = bool(_EXT_RE.search(m.group(1)))
            mask_from = m.start() if label_is_name else m.end(1)
            spans.append(_Span(m.start(), m.end(), found[0], found[1], mask_from))
    for pattern in (_BACKTICK_PATH_RE, _QUOTED_PATH_RE):
        for m in pattern.finditer(text):
            if not _TWO_PATHS_RE.search(m.group(1)) and _free(spans, *m.span()):
                spans.append(_Span(m.start(), m.end(), m.group(1), False, m.start()))
    for m in _FILE_URL_RE.finditer(text):
        if _free(spans, *m.span()):
            spans.append(_Span(m.start(), m.end(), unquote(m.group(1)), False, m.start()))
    for m in _WSFILE_RE.finditer(text):
        if _free(spans, *m.span()):
            spans.append(_Span(m.start(), m.end(), unquote(m.group(1)), True, m.start()))
    for m in _ABS_PATH_RE.finditer(text):
        if _free(spans, *m.span(1)):
            spans.append(_Span(m.start(1), m.end(1), m.group(1), False, m.start(1)))
    return sorted(s._replace(path=s.path.strip()) for s in spans if s.path.strip())


def _row_spans(row: str) -> list[_Span]:
    """The paths a table row names: those ``_path_spans`` finds, and a cell
    that holds nothing but a path, spaces and all, since the pipes mark its
    ends."""
    spans = _path_spans(row)
    at = 0
    for cell in row.split("|"):
        m = _CELL_PATH_RE.fullmatch(cell)
        if m and not _TWO_PATHS_RE.search(m.group(1)):
            start, end = at + m.start(1), at + m.end(1)
            if _free(spans, start, end):
                spans.append(_Span(start, end, m.group(1), False, start))
        at += len(cell) + 1
    return sorted(spans)


def _masked(text: str, spans: list[_Span]) -> str:
    """``text`` with each path's own text, and each web address (past an
    s3:// scheme), hidden behind a run of NULs of the same length, so the
    words of a file name or an address ("example.com") read as neither a
    negation nor a claim and every position stays put."""
    chars = list(text)
    hidden = [(s.mask_from, s.end) for s in spans] + [
        (m.start() + (5 if m.group(0)[:5].lower() == "s3://" else 0), m.end())
        for m in _URL_RE.finditer(text)
    ]
    for start, end in hidden:
        chars[start:end] = _MASK * (end - start)
    return "".join(chars)


def claimed_paths(text: str) -> list[str]:
    """File paths the text names as deliverables, in order, deduplicated."""
    out: list[str] = []
    for s in _path_spans(text or ""):
        if s.path not in out:
            out.append(s.path)
    return out


def _file_table_rows(lines: list[str]) -> dict[int, bool]:
    """Line index to True for each body row of a markdown table whose header
    names a file, path, location, output or saved column, and to False for
    that table's header and rule lines. The lines of any other table are left
    out, so they read as prose."""
    rows: dict[int, bool] = {}
    i = 0
    while i + 1 < len(lines):
        head, rule = lines[i], lines[i + 1]
        if "|" not in head or "|" not in rule or not _TABLE_RULE_RE.fullmatch(rule):
            i += 1
            continue
        end = i + 2
        while end < len(lines) and "|" in lines[end]:
            end += 1
        if _FILE_COLUMN_RE.search(head):
            rows.update(dict.fromkeys((i, i + 1), False))
            rows.update(dict.fromkeys(range(i + 2, end), True))
        i = end
    return rows


# ── Reading, clause by clause ─────────────────────────────────────────────
# Rows of a table that lists files: a row that says its file was deleted
# claims it is gone; one that says it moved or was renamed claims nothing
# (its old and new names share the row).
_ROW_GONE_RE = re.compile(r"\b(?:deleted|removed|trashed)\b", re.IGNORECASE)
# "server/app.py was updated": the verb after the path.
# Between a headline's verb and its path: where the work went ("Report saved
# to X", "Changes written: X").
_HEADLINE_PLACE_RE = re.compile(r"^[\s*_]*(?:to|into|in|at|as|under|on|:)", re.IGNORECASE)
# "`old/helper.py` is gone now": what now is.
_GONE_AFTER_RE = re.compile(
    rf"^[\s`*_\"')\]]*(?:(?:is|are|{_APOS}s)\s+(?:now\s+)?(?:gone|deleted|removed)"
    r"|no\s+longer\s+exists)\b",
    re.IGNORECASE,
)
# "`docs/setup.md` now has the install steps": a passive in other words.
_NOW_HAS_RE = re.compile(
    r"^[\s`*_\"')\]]*now\s+(?:has|have|contains|includes|lists|shows|holds|uses|handles"
    r"|supports|reads|returns|logs|retries)\b",
    re.IGNORECASE,
)
_PASSIVE_AFTER_RE = re.compile(
    rf"^[\s`*_\"')\]]*(?:was|were|has been|have been|is now|are now|got|{_APOS}s been)"
    rf"(?:\s+(?:also|now|just|successfully|finally))*\s+(?P<verb>{_MADE_VERBS}|{_GONE_VERBS})\b",
    re.IGNORECASE,
)

# A path right after "from" is where the work came from ("Added the rows from
# `a.md`"); after a verb of removing, a path after "from", "across", "in" and
# the like was edited, not removed ("Removed unused imports across
# `server/app.py`").
_FROM_AHEAD_RE = re.compile(r"\bfrom\s*[`'\"(\[*_]*$", re.IGNORECASE)
_EDIT_PREP_RE = re.compile(
    r"\b(?:from|across|of|inside|within|in|throughout|out\s+of)\s*[`'\"(\[*_]*$", re.IGNORECASE
)
# "Renamed `old.ts` to `new.ts`": the first path is gone, the second made.
_RENAME_VERB_RE = re.compile(r"^(?:renamed|moved)$", re.IGNORECASE)
_RENAME_TO_RE = re.compile(r"^[\s`'\"*_)\]]*(?:to|into|as)\b", re.IGNORECASE)
# The tail of a path with spaces written bare ("~/My Folder/a.py" read as
# "~/My" and "Folder/a.py"): neither part is the path, so neither is read.
_SPACED_PATH_AHEAD_RE = re.compile(r"(?:~|/)[^\s`'\"]*\s$")
_SPACED_PATH_AFTER_RE = re.compile(r"\s[^\s/`'\"]+/")


class _Found(NamedTuple):
    """A path a clause names, and what it is: a document (``_path_spans``),
    another file, or a folder."""

    span: _Span
    deliverable: bool
    folder: bool


def _other_paths(clause: str, docs: list[_Span]) -> list[_Found]:
    """The files and folders ``clause`` names besides its documents
    (``docs``), outside any web address, in text order."""
    taken = list(docs) + [_Span(*m.span(), "", False, m.start()) for m in _URL_RE.finditer(clause)]
    found: list[_Found] = []
    for pattern, folder in ((_OTHER_FILE_RE, False), (_FOLDER_RE, True)):
        for m in pattern.finditer(clause):
            path = m.group(1).rstrip(" ")
            start, end = m.start(1), m.start(1) + len(path)
            if not path or not _free(taken, start, end):
                continue
            if folder and (_FILE_NAME_RE.search(path) or _SPACED_PATH_AFTER_RE.match(clause[end:])):
                continue
            if not path.startswith(("~", "/")) and _SPACED_PATH_AHEAD_RE.search(clause[:start]):
                continue
            span = _Span(start, end, path, False, start)
            taken.append(span)
            found.append(_Found(span, False, folder))
    return sorted(found, key=lambda f: f.span.start)


def _last_verb(ahead: str):
    """The last verb of making or removing in ``ahead``, or None."""
    last = None
    for m in _VERB_RE.finditer(ahead):
        last = m
    return last


def _made_verb(said: str) -> bool:
    return any(m.group("made") for m in _VERB_RE.finditer(said))


def _path_claims(
    lead: str,
    masked: str,
    said: str,
    found: list[_Found],
    prev: tuple[bool, bool, bool],
    negated: bool,
) -> list[Claim]:
    """The paths one clause claims. A document is claimed as the notes above
    _DELIVERABLE_EXTS say; any other file or folder only where the assistant
    says it made, changed or removed it ("firm"), where a passive says so of
    it (checked only if the turn wrote it), where it now is gone, or where it
    is an item under a lead line that says so ("I updated these files:"). A
    headline whose verb takes the path as its object ("The PR updated
    `server/app.py`") names who did it, and a dated description ("the
    migration added `x.py` two days ago") claims nothing. A clause of paths
    alone continues the last one ("Saved to X, Y and Z")."""
    prev_claimed, prev_made, prev_firm = prev
    others = bool(OTHERS_WORK_RE.search(lead))
    listed = _LEAD_MADE_RE.match(lead)
    done = bool(_DONE_RE.search(said))
    made_here = _made_verb(said) and not _STATE_RE.search(said)
    continuing = prev_claimed and not _PATHS_ONLY_RE.sub("", masked).strip()
    claims: list[Claim] = []
    for i, f in enumerate(found):
        s = f.span
        ahead = masked[: s.start]
        if _OFFER_AHEAD_RE.search(f"{lead} {ahead}"):
            continue
        # A passive right after the path is its own verb ("`a.py` was updated
        # and `b.md` was removed"); else the last verb ahead of it.
        passive = _PASSIVE_AFTER_RE.match(masked[s.end :])
        verb = None if passive else _last_verb(ahead)
        gone = bool(verb and verb.group("gone")) or bool(
            passive and re.fullmatch(_GONE_VERBS, passive.group("verb"), re.IGNORECASE)
        )
        located = bool(_LOCATED_RE.search(_LINK_OPEN_RE.sub("", ahead)))
        if _FROM_AHEAD_RE.search(ahead) and not gone:
            # "Added the rows from `a.md`": where the work came from.
            continue
        if gone and (located or _EDIT_PREP_RE.search(ahead)):
            gone = False
        renamed_from = (
            verb is not None
            and _RENAME_VERB_RE.match(verb.group(0))
            and i + 1 < len(found)
            and _RENAME_TO_RE.match(masked[s.end :])
        )
        firm = True
        after = masked[s.end :]
        gone_now = None if verb or passive else _GONE_AFTER_RE.match(after)
        now_has = None if verb or passive or gone_now else _NOW_HAS_RE.match(after)
        item = listed is not None and not re.sub(
            r"[\W_]", "", _LIST_ITEM_RE.sub("", ahead, count=1)
        )
        if gone_now or (item and listed.group("gone")):
            gone = True
        if f.deliverable:
            if negated or not (
                s.app_link or done or located or continuing or gone or passive or item
            ):
                continue
        elif verb is not None:
            if _NOT_MADE_RE.search(f"{lead} {ahead}"):
                continue
            who = _subject(ahead[: verb.start()], passive=False)
            if who is None or _BY_OTHER_RE.search(masked[s.end :]):
                continue
            if who == "headline" and not _HEADLINE_PLACE_RE.match(ahead[verb.end() :]):
                # "The PR updated `server/app.py`": the path is the object of
                # the noun's own act, not a place the work went to.
                continue
            if who != "firm" and _DATED_RE.search(masked):
                continue
            firm = who != "soft"
        elif passive or now_has:
            if _BY_OTHER_RE.search(masked[s.end :]):
                continue
            firm = False
        elif gone_now:
            firm = not negated
        elif item:
            firm = not negated
        elif continuing:
            firm = prev_firm
        else:
            continue
        kind = REMOVED if (gone or renamed_from) else (FOLDER if f.folder else FILE)
        made = kind != REMOVED and (
            made_here or bool(passive) or bool(now_has) or item or (continuing and prev_made)
        )
        claims.append(
            Claim(
                kind,
                s.path,
                made=made,
                deliverable=f.deliverable,
                firm=(firm or f.deliverable) and not others,
            )
        )
    return claims


def _row_claims(lead: str, row: str) -> list[Claim]:
    """The claims a row of a file table makes: every path in it, unless the
    row (with the table's lead-in line) negates, offers or asks, or says its
    file moved. A row that says its file was deleted claims it is gone. A
    file that is not a document is claimed only where the row or its lead
    says what was done to it, and then only where the turn wrote it (a table
    reviewing a pull request lists changes someone else made) unless the lead
    says the assistant did it; a document only says where it is unless they
    do."""
    docs = _row_spans(row)
    others = _other_paths(row, docs)
    masked = _masked(row, docs + [f.span for f in others])
    said = f"{lead} {masked}"
    if (
        any(_is_question(cell) for cell in said.split("|"))
        or _NOT_MADE_RE.search(said)
        or _OFFER_AHEAD_RE.search(said)
    ):
        return []
    gone = bool(_ROW_GONE_RE.search(masked))
    if not gone and _GONE_RE.search(masked):
        return []
    made = not gone and _made_verb(said) and not _STATE_RE.search(said)
    kind = REMOVED if gone else FILE
    claims = [Claim(kind, s.path, made=made, deliverable=True) for s in docs]
    if gone or made:
        # Under a lead in which the assistant says it changed them ("I
        # updated these files:"), the rows are its own claims.
        mine = bool(_LEAD_MADE_RE.match(lead)) and not OTHERS_WORK_RE.search(lead)
        claims += [
            Claim(
                REMOVED if gone else (FOLDER if f.folder else FILE),
                f.span.path,
                made=made,
                firm=mine,
            )
            for f in others
        ]
    return claims
