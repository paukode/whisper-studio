"""Files a clause names by their name alone (read for server/goals/claims.py).

The path readers (claim_paths.py) need a folder in a path: a bare name such
as `inside.txt` names no place, so "I created `inside.txt`" and "The refresh
saved `snapshot.json`" went unread. On the user's sessions those were the
only file claims the reader missed (3 of 7,846 sentences, all true). Two
forms are read here, and nothing else:

- a past verb of delivery (saved, wrote, created, exported, generated,
  built, downloaded, copied, moved) whose object is the name, said by the
  assistant, with no subject, or with the turn's own process as subject
  ("The refresh saved `x.json`", "..., which created `inside.txt`"): never a
  person's ("Dana saved `x.md`"), a plan, an offer, a negation or a question;
- the name, then "is in", "is written to" or the like, then a place:
  "`app.dmg` is in ~/Downloads", "`built.md` is written to Downloads".

A place after the name ("to Downloads", "in your workspace root", "in
`~/out/`") gives the claim its folder. With none, the claim is ``bare``: the
check looks for the name in the folders the turn itself wrote to or named
(server/goals/evidence.py). A name inside inline code counts only where the
code is the name alone, so `echo hi > inside.txt` claims nothing.

Pure functions over text; no I/O.
"""

from __future__ import annotations

import re

from server.goals.claim_text import (
    _APOS,
    _BY_OTHER_RE,
    _OFFER_AHEAD_RE,
    _PROCESS_RE,
    FILE,
    Claim,
    _subject,
)

# File types a reply hands over or a turn writes: documents, data, images,
# media, archives, installers, notebooks and scripts. A name with any other
# ending ("server.main", "Node.js") is not read as a file.
_NAME_EXTS = (
    r"docx|xlsx|pptx|pdf|html?|md|csv|json|txt|png|svg|zip|dmg|ya?ml|xml|log|jpe?g|gif|webp"
    r"|mp3|mp4|m4a|wav|ics|tar|gz|tgz|pkg|ipynb|sql|py|js|ts|tsx|sh|toml"
)
_NAME_RE = re.compile(
    rf"(?<![\w/.~@:%\\-])([\w@+-][\w@+.-]*\.(?:{_NAME_EXTS}))(?![\w/-])", re.IGNORECASE
)
_CODE_RE = re.compile(r"`([^`\n]+)`")

_DELIVER_RE = re.compile(
    r"\b(?:saved|wrote|written|created|exported|generated|built|downloaded|copied|moved)\b",
    re.IGNORECASE,
)
# Between the verb and the name: a few words of object, and "as", "to",
# "into", "named" or "called" ("saved the snapshot as `x.json`"). A word that
# makes the name a source or a part ("from", "of", "with") ends the reading.
_GAP_RE = re.compile(
    r"^(?:\s+[\w'\N{RIGHT SINGLE QUOTATION MARK}-]+){0,4}?(?:\s+(?:as|to|into|named|called))?"
    r"[\s`*_\"']*$",
    re.IGNORECASE,
)
_GAP_STOP_RE = re.compile(
    r"\b(?:from|of|with|for|by|using|via|than|and|or|in|on|at|not|no)\b", re.IGNORECASE
)
# The turn's own process as subject: "The refresh", "the build script",
# "which" after a comma, "it", each maybe after a verb it did first ("The
# refresh completed successfully and").
_PROCESS_SUBJECT_RE = re.compile(
    r"^[\s\W]*(?:(?:the|this|that|my|our)\s+(?:[\w-]+\s+)?(?:refresh|build|script|export"
    r"|command|job|run|tool|pipeline|generator|step|task|process|conversion|render|backup"
    r"|download|sync|import|migration|workflow|agent|subagent|call)|which|it)"
    r"(?:\s+(?:also|then|just|now|successfully|finally))*"
    r"(?:\s+[a-z]+ed(?:\s+successfully)?\s+(?:and|&)(?:\s+(?:then|also))?)?\s*$",
    re.IGNORECASE,
)
# The name, then where it is: "is in", "is now saved at", "was written to".
_STATE_AFTER_RE = re.compile(
    rf"^[\s`*_\"')\]]*(?:is|are|was|were|has\s+been|have\s+been|{_APOS}s)"
    r"(?:\s+(?:now|also|already|successfully|safely|all))*"
    r"\s+(?:(?P<made>saved|written|stored|exported|copied|moved|built|created|generated"
    r"|downloaded|placed|put)\s+)?(?:to|in|into|at|under|inside)\b",
    re.IGNORECASE,
)
# A place named after the name: a home folder by its word, or the workspace.
_PLACE_WORD_RE = re.compile(
    r"^[\s`*_\"']*(?:(?:to|in|into|at|under|inside)\s+)?(?:(?:your|the|my)\s+)?"
    r"(?:(?P<home>downloads|desktop|documents)\b(?:\s+folder)?"
    r"|(?P<ws>workspace)(?:\s+(?:root|folder))?\b)",
    re.IGNORECASE,
)
_PLACE_PREP_RE = re.compile(r"\b(?:to|in|into|at|under|inside)\s*[`*_\"']*$", re.IGNORECASE)
_HOME_FOLDERS = {"downloads": "~/Downloads", "desktop": "~/Desktop", "documents": "~/Documents"}


def _names(clause: str, masked: str) -> list[re.Match]:
    """The bare names of ``masked`` (paths and addresses already hidden), a
    name inside inline code only where the code is the name alone."""
    code = [(m.start(), m.end(), m.group(1).strip()) for m in _CODE_RE.finditer(clause)]
    out = []
    for m in _NAME_RE.finditer(masked):
        inside = next((c for c in code if c[0] < m.start(1) and m.end(1) < c[1]), None)
        if inside is None or inside[2] == m.group(1):
            out.append(m)
    return out


def _place(
    after: str, folders: list[tuple[int, int, str]], at: int, *, prep: bool = False
) -> str | None:
    """The folder named in ``after`` (the clause from offset ``at`` on): a
    home folder's word, "workspace" ("" for the workspace itself), or a
    folder path after "to", "in" or the like (with ``prep``, the word that
    leads to the place was already read); None when no place is named."""
    word = _PLACE_WORD_RE.match(after)
    if word:
        return _HOME_FOLDERS[word.group("home").lower()] if word.group("home") else ""
    for start, _end, path in folders:
        if start < at:
            continue
        between = after[: start - at]
        if (prep and not between.strip(" `*_\"'")) or (
            _PLACE_PREP_RE.search(between) and len(between.split()) <= 6
        ):
            return path.rstrip("/")
    return None


def _target(name: str, place: str | None) -> tuple[str, bool]:
    """The claim's target and whether it is bare (no folder known)."""
    if place is None:
        return name, True
    return (f"{place}/{name}" if place else name), False


def name_claims(
    lead: str,
    clause: str,
    masked: str,
    named: list[str],
    folders: list[tuple[int, int, str]],
    negated: bool,
) -> list[Claim]:
    """The file claims one clause makes by a bare name (see the module
    notes). ``named`` are the paths the clause already claims or names, so a
    name they hold is not read twice; ``folders`` the folder paths it names,
    as (start, end, path)."""
    if negated or _PROCESS_RE.search(masked):
        return []
    claims: list[Claim] = []
    for m in _names(clause, masked):
        name = m.group(1)
        if any(p.rstrip("/").split("/")[-1] == name for p in named):
            continue
        ahead, after = masked[: m.start(1)], masked[m.end(1) :]
        if _OFFER_AHEAD_RE.search(f"{lead} {ahead}") or _BY_OTHER_RE.search(after):
            continue
        state = _STATE_AFTER_RE.match(after)
        if state:
            # "`app.dmg` is in ~/Downloads": where it is, with the place named.
            place = _place(after[state.end() :], folders, m.end(1) + state.end(), prep=True)
            if place is None:
                continue
            target, bare = _target(name, place)
            claims.append(Claim(FILE, target, made=bool(state.group("made")), bare=bare))
            continue
        verbs = list(_DELIVER_RE.finditer(ahead))
        if not verbs:
            continue
        verb = verbs[-1]
        gap = ahead[verb.end() :]
        if not _GAP_RE.match(gap) or _GAP_STOP_RE.search(gap):
            continue
        before = ahead[: verb.start()]
        if _subject(before, passive=False) != "firm" and not _PROCESS_SUBJECT_RE.match(before):
            continue
        target, bare = _target(name, _place(after, folders, m.end(1)))
        claims.append(Claim(FILE, target, made=True, bare=bare))
    return claims
