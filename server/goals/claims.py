"""What a reply says it delivered, read clause by clause.

A reply reports what the turn did: "Saved to `~/Downloads/report.docx`",
"Pushed the fix to `origin/main`", "Opened PR #12", "Sent the summary to
Dana". Each such statement claims that something now exists or happened. In
real sessions a saved file did not exist and the user found out only by
asking, so every claim is checked before the user reads it: the stream guard
(server/chat/claim_guard.py) holds the sentence that makes one until
server/goals/evidence.py has verified it, and the completion gate
(deliverables.check_claims) sends the model back when it cannot be.

This module only reads text. A claim is a statement, not a plan, an offer, a
supposition, an example, a negation or a question, and never code, quoted
text or a draft the reply hands over (claim_text.Lines). Each carries the
offsets of the clause that makes it, so the guard knows which sentence to
hold. The words are in claim_text.py, the reading of paths in
claim_paths.py and of acts in claim_acts.py.

The kinds: a file saved (``FILE``; ``deliverable`` marks a finished document
such as a .docx or .pdf at an absolute or home path), a folder written to
(``FOLDER``), a file removed (``REMOVED``), an artifact card, a git push,
commit or merge, a pull request, an issue, an upload, a message or email
sent, something published or deployed, a task scheduled, a link created, a
memory saved and a setting changed. A code or other file that is not a
document counts only where a clause says the assistant itself made, changed
or removed it ("I updated `server/app.py`", "Removed `old/helper.py`"):
naming one while describing work claims nothing. The kinds that are not
files need the same first-person or agentless statement ("Pushed to `main`",
"The email was sent"), never a third party's ("CI pushed a build", "Dana
merged it").

Pure functions over text; no I/O.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import replace

from server.goals.claim_acts import act_claims
from server.goals.claim_paths import (
    _file_table_rows,
    _Found,
    _free,
    _masked,
    _other_paths,
    _path_claims,
    _path_spans,
    _row_claims,
    claimed_paths,
)
from server.goals.claim_text import (
    _CONDITIONAL_RE,
    _FIRST_PERSON_RE,
    _GONE_VERBS,
    _LIST_ITEM_RE,
    _MADE_VERBS,
    _NOT_MADE_RE,
    _REPORTED_RE,
    _SENTENCE_BREAK_RE,
    ARTIFACT,
    COMMIT,
    FILE,
    FOLDER,
    ISSUE,
    LINK,
    MEMORY,
    MERGE,
    MESSAGE,
    PR,
    PUBLISH,
    PUSH,
    REMOVED,
    SCHEDULE,
    SETTING,
    UPLOAD,
    Claim,
    _is_question,
    _readable,
    _reading_copy,
    points_back,
)

__all__ = [
    "ARTIFACT", "COMMIT", "FILE", "FOLDER", "ISSUE", "LINK", "MEMORY", "MERGE", "MESSAGE", "PR",
    "PUBLISH", "PUSH", "REMOVED", "SCHEDULE", "SETTING", "TRIGGER_RE", "UPLOAD", "Claim",
    "asserted_paths", "claimed_paths", "claims_an_artifact", "read_claims",
]  # fmt: skip

_CLAUSE_SPLIT_RE = re.compile(
    r"(?<=[.!?])\s+|[;,]\s+|\s+[-\u2013]\s+"
    r"|(?<=[\N{IDEOGRAPHIC FULL STOP}\N{FULLWIDTH EXCLAMATION MARK}\N{FULLWIDTH QUESTION MARK}])"
    r"|\s+(?:but|though|although|however|so|then|while|whereas)\s+",
    re.IGNORECASE,
)

# What may make a sentence still being written a claim, for the stream guard
# (server/chat/claim_guard.py): a path, an address, a link, code, a number
# sign, or any verb this module reads.
TRIGGER_RE = re.compile(
    r"[~/`\[#]|://|\b\w+/\w"
    rf"|\b(?:{_MADE_VERBS}|{_GONE_VERBS}|pushed|committed|commits?|merged|opened|raised|submitted"
    r"|filed|logged|uploaded|synced|sent|emailed|e-mailed|messaged|posted|replied|forwarded"
    r"|pinged|texted|notified|dm\w*|deployed|redeployed|published|released|scheduled|set|switched"
    r"|enabled|disabled|toggled|turned|noted|recorded|kept|made|artifact|here|ready|available"
    r"|find|located|lives|live|PRs?|pull request|memory|shipped|rolled|deploy\w*|gone|let"
    r"|commented|left|approved|reminders?|cron|issues?|origin|upstream|main|master|theme|mode"
    r"|effort|notifications?|status)\b",
    re.IGNORECASE,
)


def _clause_offsets(line: str) -> list[tuple[int, int]]:
    """Where each clause of one line starts and ends. A split that falls
    inside a path's mention (a comma or " - " in a quoted file name) is part
    of the name."""
    spans = _path_spans(line)
    out: list[tuple[int, int]] = []
    start = 0
    for m in _CLAUSE_SPLIT_RE.finditer(line):
        if _free(spans, *m.span()):
            out.append((start, m.start()))
            start = m.end()
    out.append((start, len(line)))
    return out


def _paths_only(line: str) -> bool:
    """True when ``line`` names one or more paths and says nothing else."""
    docs = _path_spans(line)
    spans = docs + [f.span for f in _other_paths(line, docs)]
    return bool(spans) and not re.search(r"[A-Za-z]", _masked(line, spans))


def _clause_spans(text: str):
    """(lead, start, end, file_row) for each clause of each line outside code
    blocks, quotes and drafts (claim_text.Lines), as offsets into ``text``.
    The lead is the line that introduces a list or a table, for its items
    (``_readable``), and stays across a blank line; a row of a table that
    lists files is one clause of its own (``file_row``)."""
    work = _reading_copy(text)
    lines: list[tuple[int, str]] = []
    at = 0
    for line in work.split("\n"):
        lines.append((at, line))
        at += len(line) + 1
    table = _file_table_rows([line for _at, line in lines])
    lead = ""
    for i, (at, line) in enumerate(lines):
        if not line.strip() or table.get(i) is False:
            continue
        if table.get(i):
            yield lead, at, at + len(line), True
            continue
        # A list item, or a line of paths alone under a lead line ("Saved
        # the report to:" then the path on a line of its own).
        item = bool(_LIST_ITEM_RE.match(line)) or bool(
            lead and _paths_only(text[at : at + len(line)])
        )
        if not item:
            lead = _readable(text[at : at + len(line)]) if line.rstrip().endswith(":") else ""
        for start, end in _clause_offsets(line):
            if line[start:end].strip():
                yield (lead if item else ""), at + start, at + end, False


def _clause_claims(
    lead: str, clause: str, prev: tuple[bool, bool, bool]
) -> tuple[list[Claim], tuple[bool, bool, bool]]:
    """The claims one clause of prose makes, and whether it claimed a path
    (a made one, a firm one) for a next clause of paths alone."""
    docs = _path_spans(clause)
    found = sorted(
        [_Found(s, True, False) for s in docs] + _other_paths(clause, docs),
        key=lambda f: f.span.start,
    )
    masked = _masked(clause, [f.span for f in found])
    said = f"{lead} {masked}"
    if _is_question(clause):
        return [], (False, False, False)
    # A document's clause is negated whole (claim_paths._path_claims);
    # an act or another file is negated only around its own verb, so "Pushed
    # the fix for the Not Found case" is still a push.
    negated = bool(_NOT_MADE_RE.search(said))
    paths = _path_claims(lead, masked, said, found, prev, negated) if found else []
    acts = act_claims(lead, clause, masked)
    after = (bool(paths), any(p.made for p in paths), any(p.firm for p in paths))
    return paths + acts, after


def _sentence(text: str, start: int, end: int, breaks: list[int]) -> str:
    """The sentence of ``text`` that holds [start, end)."""
    i = bisect_right(breaks, start)
    begin = breaks[i - 1] if i else 0
    j = bisect_left(breaks, end)
    finish = breaks[j] if j < len(breaks) else len(text)
    return text[begin:finish]


def read_claims(text: str) -> list[Claim]:
    """Every delivery ``text`` claims, in text order (see the module notes),
    each with the offsets of the clause that makes it. A clause that points
    back to an earlier turn marks its claims ``earlier``; a sentence about
    what happens when something else does ("When you run it, ...", "On
    failure, ...") claims nothing, unless the clause that claims is the
    assistant's own ("After the tests passed, I pushed")."""
    text = text or ""
    out: list[Claim] = []
    prev = (False, False, False)
    breaks = [m.end() for m in _SENTENCE_BREAK_RE.finditer(_reading_copy(text))]
    for lead, start, end, file_row in _clause_spans(text):
        clause = _readable(text[start:end])
        if file_row:
            found = _row_claims(lead, clause)
            prev = (bool(found), any(x.made for x in found), any(x.firm for x in found))
        else:
            found, prev = _clause_claims(lead, clause, prev)
        if not found:
            continue
        sentence = _sentence(text, start, end, breaks)
        if _CONDITIONAL_RE.match(sentence) and not _FIRST_PERSON_RE.search(clause):
            continue
        at = bisect_right(breaks, start)
        head = text[breaks[at - 1] if at else 0 : start]
        earlier = points_back(lead, clause, head)
        reported = bool(_REPORTED_RE.search(sentence))
        out += [
            replace(
                x,
                start=start,
                end=end,
                clause=text[start:end],
                earlier=x.earlier or earlier,
                firm=x.firm and not (reported and not x.deliverable),
            )
            for x in found
        ]
    return out


def asserted_paths(text: str) -> list[str]:
    """The document paths ``text`` says were made or points at where they
    are (the ``deliverable`` files of ``read_claims``), in order,
    deduplicated."""
    out: list[str] = []
    for x in read_claims(text):
        if x.kind == FILE and x.deliverable and x.target not in out:
            out.append(x.target)
    return out


def claims_an_artifact(text: str) -> bool:
    """True when ``text`` points at an artifact card as made, as opposed to
    offering one, asking about one or saying there is none."""
    return any(x.kind == ARTIFACT for x in read_claims(text))
