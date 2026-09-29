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
supposition, an example, a negation or a question (the notes above
_CODE_FENCE_RE). Each carries the offsets of the clause that makes it, so the
guard knows which sentence to hold.

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
from dataclasses import dataclass, replace
from typing import NamedTuple
from urllib.parse import unquote

FILE = "file"
FOLDER = "folder"
REMOVED = "removed"
ARTIFACT = "artifact"
PUSH = "push"
COMMIT = "commit"
PR = "pr"
MERGE = "merge"
ISSUE = "issue"
UPLOAD = "upload"
MESSAGE = "message"
PUBLISH = "publish"
SCHEDULE = "schedule"
LINK = "link"
MEMORY = "memory"
SETTING = "setting"


@dataclass(frozen=True)
class Claim:
    """One thing a reply says was delivered.

    ``target`` is what the clause names (a path, a URL, a branch, a number, a
    recipient), or "" when it names none. ``made`` is False where the clause
    only says where something is ("the report is at X"), so being there is
    enough; ``earlier`` marks a clause that points back to an earlier turn
    ("the file I saved earlier"). ``start`` and ``end`` are the clause's
    offsets in the text read, and ``clause`` its words."""

    kind: str
    target: str = ""
    made: bool = True
    earlier: bool = False
    deliverable: bool = False
    start: int = 0
    end: int = 0
    clause: str = ""


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
_ARTIFACT_CLAIM_RE = re.compile(
    r"\bartifact (?:card )?(?:above|below|attached)\b|\bin the artifact\b|\bartifact card\b",
    re.IGNORECASE,
)

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
_CODE_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_ABBREVIATIONS = {"e.g.": "for example", "i.e.": "that is"}
_ABBREV_RE = re.compile(r"\b(?:e\.g|i\.e)\.", re.IGNORECASE)
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+")
_TABLE_RULE_RE = re.compile(r"\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*")
_FILE_COLUMN_RE = re.compile(
    r"\b(?:file(?:name|path)?s?|paths?|locations?|outputs?|saved)\b", re.IGNORECASE
)
_GONE_RE = re.compile(r"\b(?:deleted|removed|trashed|moved|renamed)\b", re.IGNORECASE)
_CLAUSE_SPLIT_RE = re.compile(
    r"(?<=[.!?])\s+|[;,]\s+|\s+[-\u2013]\s+"
    r"|\s+(?:but|though|although|however|so|then|while|whereas)\s+",
    re.IGNORECASE,
)
_DONE_RE = re.compile(
    r"\b(?:saved|wrote|written|created|exported|generated|produced|stored|placed|put"
    r"|rendered|built|converted|updated|downloaded|attached|ready|available|here|find"
    r"|located|lives)\b|\b(?:is|are) (?:now )?(?:at|in)\b",
    re.IGNORECASE,
)
_LOCATED_RE = re.compile(r"(?:\b(?:to|at|in|as|into|under)|:)[\s`*_\"'(\[]*$", re.IGNORECASE)
_LINK_OPEN_RE = re.compile(r"\[[^\]]*\]\(\s*$")
_NOT_MADE_RE = re.compile(
    r"\b(?:not|never|unable|failed|cannot|no longer|would|could|might|for example"
    r"|such as|example)\b|n['\u2019]t\b",
    re.IGNORECASE,
)
_OFFER_AHEAD_RE = re.compile(
    r"\b(?:i|we)(?:['\u2019](?:ll|d)|\s+(?:will|shall|may|am going to|are going to))\b"
    r"|\b(?:i|we)\s+can\b(?!\s+(?:confirm|see|tell|verify))"
    r"|\b(?:i|we)['\u2019](?:m|re)\s+going to\b"
    r"|\blet me\b(?!\s+know)"
    r"|\b(?:shall i|should i|want me to|like me to|if you|will be|going to be|no)\b",
    re.IGNORECASE,
)
_PATHS_ONLY_RE = re.compile(r"[^A-Za-z]+|\band\b|\bor\b", re.IGNORECASE)
_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s<>()\[\]`\"']+", re.IGNORECASE)
_MASK = "\0"

# ── Beyond documents ───────────────────────────────────────────────────────
# Any other file: under a local root or the home folder, or relative with a
# folder in it ("server/app.py", which reads against the workspace), with an
# extension; a hidden file at the home folder ("~/.zshrc"). A folder is a
# home or rooted path whose last part has no extension ("~/Downloads",
# "/Users/me/Projects/app/"). An absolute path counts only under a local root:
# "/api/v1/users" is a route, not a place on disk.
_APOS = "['\N{RIGHT SINGLE QUOTATION MARK}]"
_ROOT = r"(?:~|/(?:Users|Volumes|private|tmp|var|opt|Applications|Library|home|mnt|srv)(?=/|\b))"
_SEGMENT = r"[\w@+-][\w.@+-]*"
_OTHER_FILE_RE = re.compile(
    r"(?<![\w/.~@:%-])("
    rf"{_ROOT}/(?:{_SEGMENT}/)*{_SEGMENT}\.[A-Za-z0-9]{{1,10}}"
    rf"|(?:\./)?(?:{_SEGMENT}/)+{_SEGMENT}\.[A-Za-z0-9]{{1,10}}"
    r"|~/\.[\w.@+-]+"
    r")(?![\w/])"
)
_FOLDER_RE = re.compile(
    rf"(?<![\w/.~@:%-])({_ROOT}(?:/[\w@+-][\w@+ -]*?)*/?)(?=[\s`'\".,;:)!?*_]|$)"
)
_FILE_NAME_RE = re.compile(r"\.[A-Za-z0-9]{1,10}$")

# The verbs that say the assistant made, changed or removed a file, and the
# words that say it only is somewhere ("is saved at", "is in").
_MADE_VERBS = (
    r"saved|wrote|written|rewrote|rewritten|created|exported|generated|produced|stored|placed"
    r"|put|rendered|built|converted|updated|downloaded|attached|copied|moved|renamed|edited"
    r"|modified|changed|fixed|added|refactored|patched|replaced|appended|inserted|restored"
    r"|drafted|compiled"
)
_GONE_VERBS = r"deleted|removed|trashed|erased"
_VERB_RE = re.compile(rf"\b(?:(?P<gone>{_GONE_VERBS})|(?P<made>{_MADE_VERBS}))\b", re.IGNORECASE)
_STATE_RE = re.compile(
    rf"\b(?:is|are|{_APOS}s)\s+(?:(?:now|still|also|already)\s+)*"
    r"(?:saved|stored|located|kept|placed|available|ready|in|at|under|inside)\b",
    re.IGNORECASE,
)
_EARLIER_RE = re.compile(
    r"\b(?:earlier|previously|already|yesterday|as before|before (?:this|now)"
    r"|last (?:time|turn|session|week|night)"
    r"|(?:in|from|during) (?:the|a|an|my|our|your) (?:previous|earlier|last|prior) \w+)\b",
    re.IGNORECASE,
)

# Whose statement it is. Before the verb there may be connectors ("Done:",
# "Also"), then "I" or "we" with helpers ("I've also", "we just"), or
# nothing ("Pushed to main"); or a subject with an event passive ("the
# branch was pushed", "it has been sent"). A plain "is" or "are" passive
# ("the changes are pushed") counts only for the kinds where it reports the
# assistant's own act.
_SELF_SUBJECT_RE = re.compile(
    r"^[\s\W]*"
    r"(?:(?:and|also|then|finally|done|ok|okay|great|now|just|so|next|lastly|first|second"
    r"|third|plus|all set|as requested|as asked|good news|update|result|summary)\b[\s\W]*)*"
    rf"(?:(?:i|we)(?:{_APOS}(?:ve|d|m|re))?"
    r"(?:\s+(?:have|had|also|just|now|then|already|successfully|finally|quickly"
    r"|went ahead and|was able to|were able to|managed to|did))*\s*)?$",
    re.IGNORECASE,
)
_EVENT_PASSIVE_RE = re.compile(
    rf"(?:\b(?:was|were|has been|have been|had been|got|gets|is now|are now)|{_APOS}s(?:\s+now)?\s+been)"
    r"(?:\s+(?:now|also|just|successfully|already|finally|all))*\s*$",
    re.IGNORECASE,
)
_STATE_PASSIVE_RE = re.compile(
    rf"(?:\b(?:is|are)|{_APOS}s|{_APOS}re)(?:\s+(?:now|also|just|all|already))*\s*$", re.IGNORECASE
)
# A third party did it: "merged by Dana", "pushed by the CI".
_BY_OTHER_RE = re.compile(
    r"\bby\s+(?:@\w|[A-Z][\w-]*|(?i:the\s+(?:user|team|ci|bot|pipeline|workflow|action"
    r"|scheduler|reviewer|maintainer)s?\b|ci\b|someone\b|another\b))"
)

# The kinds that are not files, each the past-tense verb that reports it.
# PR, issue and merge need their object in the clause ("merged the PR",
# "merged into main"; not "merged the two lists"); a message needs a message
# or a recipient ("sent the summary to Dana"; not "sent a request").
_MERGE_OBJECT = (
    r"\b(?:PRs?|pull[- ]requests?|MRs?|merge[- ]requests?|branch(?:es)?|worktrees?)\b|#\d+"
    r"|\binto\s+[`'\"]?(?:main|master|develop|dev|trunk|release)\b"
)
_KIND_PATTERNS = (
    (PUSH, re.compile(r"\b(?:force[- ]?)?pushed\b(?!\s+back\b)", re.IGNORECASE)),
    (
        COMMIT,
        re.compile(
            r"\bcommitted\b(?!\s+to\s+\w+ing\b)"
            r"|\b(?:made|created|added)\s+(?:a|an|the|one|two|three|several|\d+)?\s*(?:new\s+)?commits?\b",
            re.IGNORECASE,
        ),
    ),
    (MERGE, re.compile(rf"\bmerged\b(?=.*?(?:{_MERGE_OBJECT}))", re.IGNORECASE)),
    (
        PR,
        re.compile(
            r"\b(?:opened|created|raised|submitted|filed|put up|made)\b"
            r"(?=.*?\b(?:PRs?|pull[- ]requests?|MRs?|merge[- ]requests?)\b)",
            re.IGNORECASE,
        ),
    ),
    (
        ISSUE,
        re.compile(
            r"\b(?:opened|created|filed|raised|logged|submitted)\b"
            r"(?=.*?\b(?:issues?|tickets?|bug reports?)\b)",
            re.IGNORECASE,
        ),
    ),
    (
        UPLOAD,
        re.compile(
            r"\buploaded\b|\b(?:copied|synced|moved|put)\b(?=.*?(?:\bs3\b|\bbuckets?\b|s3://))",
            re.IGNORECASE,
        ),
    ),
    (
        MESSAGE,
        re.compile(
            rf"\b(?:sent|emailed|e-mailed|messaged|posted|replied|forwarded|pinged|texted|notified|dm{_APOS}?e?d)\b",
            re.IGNORECASE,
        ),
    ),
    (
        PUBLISH,
        re.compile(
            r"\b(?:deployed|redeployed|published)\b"
            r"|\breleased\b(?=.*?(?:\bv?\d+\.\d+|\bversions?\b|\breleases?\b|\bpackages?\b"
            r"|\bto\s+(?:prod|production|staging|npm|pypi)\b))"
            r"|\b(?:is|are)\s+now\s+live\b|\bwent\s+live\b",
            re.IGNORECASE,
        ),
    ),
    (
        SCHEDULE,
        re.compile(
            r"\bscheduled\b(?=.*?\b(?:tasks?|jobs?|runs?|reminders?|cron|checks?|reports?|agents?"
            r"|workflows?|it|this|them)\b)"
            r"|\bset\s+up\s+(?:a|an|the)?\s*(?:daily|weekly|hourly|nightly|recurring|scheduled)?\s*"
            r"(?:cron|reminder|schedule|scheduled task|job)\b",
            re.IGNORECASE,
        ),
    ),
    (
        MEMORY,
        re.compile(
            r"\b(?:saved|stored|added|noted|recorded|written|wrote|put|kept)\b"
            r"(?=.*?\b(?:to|in|into)\s+(?:your\s+|my\s+|the\s+|long[- ]term\s+|session\s+)*memory\b)",
            re.IGNORECASE,
        ),
    ),
    (
        SETTING,
        re.compile(
            r"\b(?:set|changed|updated|switched|turned\s+(?:on|off)|enabled|disabled|toggled)\b"
            r"(?=.*?\b(?:setting|settings|preferences?|feature\s+flags?)\b)",
            re.IGNORECASE,
        ),
    ),
)
# Where "is" or "are" before the verb reports the assistant's own act.
_STATE_PASSIVE_KINDS = frozenset({PUSH, COMMIT, UPLOAD, MESSAGE})
_MESSAGE_NOUN_RE = re.compile(
    r"\b(?:e-?mails?|messages?|notes?|repl(?:y|ies)|invit(?:e|es|ation|ations)|summar(?:y|ies)"
    r"|updates?|dms?|notifications?|texts?|sms|posts?|comments?|reminders?|reports?"
    r"|announcements?|newsletters?|drafts?|letters?|responses?|recap|digest)\b",
    re.IGNORECASE,
)
_RECIPIENT_RE = re.compile(
    r"\b(?:to|cc|with)\s+(?:[\w.+-]+@[\w-]+(?:\.[\w-]+)+|@[\w.-]+|#[\w-]+|[A-Z][a-z]+(?:\s[A-Z][a-z]+)?)"
    r"|(?i:\bto\s+the\s+(?:team|channel|group|thread|list|client|customer|stakeholders?|owners?"
    r"|reviewers?|recipients?)\b|\b(?:in|on|to)\s+(?:#[\w-]+|slack|teams|discord|the\s+channel)\b)"
)
_NOT_A_MESSAGE_RE = re.compile(
    r"^\W*(?:(?:a|an|the|another|one|two|several|\d+)\s+)?(?:(?:new|test|get|post|http|api)\s+)?"
    r"(?:requests?|quer(?:y|ies)|payloads?|data|forms?|commands?|signals?|packets?|events?"
    r"|webhooks?|keystrokes?|keys|inputs?|headers?|bytes|traffic|prompts?|tokens?|calls?)\b",
    re.IGNORECASE,
)
_LINK_VERB_RE = re.compile(
    r"\b(?:created|generated|published|uploaded|posted|shared|deployed)\b", re.IGNORECASE
)
# An act with no subject ("Pushed to main") reads as the assistant's only
# where its verb goes on like a verb phrase: into an object, a place, a
# target or nothing. "Scheduled Python checks" and "Merged PRs:" are names of
# things, not acts. A message verb may take its recipient ("Emailed Dana").
_AGENTLESS_RE = re.compile(
    r"^[\s\W]*(?:(?:and|also|then|finally|done|ok|okay|great|now|just|so|next|lastly|first"
    r"|second|third|plus|all set|as requested|as asked|good news|update|result|summary)\b"
    r"[\s\W]*)*$",
    re.IGNORECASE,
)
_VERB_PHRASE_RE = re.compile(
    r"^\s*(?:$|[.,;:!?)\]`'\"(\[#@~/\N{BULLET}\0]|https?://|\d|v\d"
    r"|(?:the|a|an|it|them|this|that|these|those|your|my|our|his|her|their|all|both|each"
    r"|every|one|two|three|to|into|in|on|at|from|with|for|over|back|up|out|off|and|then|as"
    r"|successfully|PR|MR|pull request|merge request|issue|ticket)\b)",
    re.IGNORECASE,
)
_RECIPIENT_VERB_RE = re.compile(
    rf"^(?:emailed|e-mailed|messaged|pinged|texted|notified|dm{_APOS}?e?d)$", re.IGNORECASE
)
_NAME_NEXT_RE = re.compile(r"^\s*[A-Z][a-z]+\b")
_BACKTICK_TOKEN_RE = re.compile(r"`([^`\s]+)`")
_TO_TOKEN_RE = re.compile(r"\b(?:to|into|on)\s+(?:the\s+)?[`'\"]?([\w./-]+)[`'\"]?", re.IGNORECASE)
_GENERIC_BRANCHES = frozenset(
    {
        "remote",
        "origin",
        "github",
        "gitlab",
        "upstream",
        "repo",
        "repository",
        "the",
        "it",
        "branch",
        "server",
        "your",
        "my",
        "a",
        "an",
    }
)
_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_NUMBER_RE = re.compile(r"(?:#|\b(?:PR|MR|issue|pull request)\s+#?)(\d+)\b", re.IGNORECASE)
_S3_RE = re.compile(r"\bs3://[^\s`'\"()<>\[\]]+")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_HANDLE_RE = re.compile(r"(?<![\w.])[@#][\w.-]*\w")
_NAME_RE = re.compile(r"\bto\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)")
_VERSION_RE = re.compile(r"\bv?\d+\.\d+(?:\.\d+)*\b")
_TRAILING_PUNCT = ".,;:!?)]}>'\"`*"


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
    """``text`` with each path's own text, and each web address, hidden
    behind a run of NULs of the same length, so the words of a file name or
    an address ("example.com") read as neither a negation nor a claim and
    every position stays put."""
    chars = list(text)
    hidden = [(s.mask_from, s.end) for s in spans] + [m.span() for m in _URL_RE.finditer(text)]
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


def _is_question(clause: str) -> bool:
    return clause.rstrip(" \t\"')]*_`").endswith("?")


# ── Reading, clause by clause ─────────────────────────────────────────────
# Rows of a table that lists files: a row that says its file was deleted
# claims it is gone; one that says it moved or was renamed claims nothing
# (its old and new names share the row).
_ROW_GONE_RE = re.compile(r"\b(?:deleted|removed|trashed)\b", re.IGNORECASE)
# "server/app.py was updated": the verb after the path.
_PASSIVE_AFTER_RE = re.compile(
    rf"^[\s`*_\"')\]]*(?:was|were|has been|have been|is now|are now|got|{_APOS}s been)"
    rf"(?:\s+(?:also|now|just|successfully|finally))*\s+(?P<verb>{_MADE_VERBS}|{_GONE_VERBS})\b",
    re.IGNORECASE,
)
# A path right after "from" is where the work came from ("Added the rows from
# `a.md`"), except after a verb of removing: "Removed the stale line from
# ~/a.md" edited that file.
_JOINED_RE = re.compile(r"(?:\band|&|,)\s*(?:then\s+|also\s+)?$", re.IGNORECASE)
_FROM_AHEAD_RE = re.compile(r"\bfrom\s*[`'\"(\[]*$", re.IGNORECASE)
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


def _reading_copy(text: str) -> str:
    """``text`` at the same length, with each code block blanked and each
    abbreviation's dots hidden, so its lines and clauses keep the offsets of
    ``text`` and "e.g." ends no sentence."""
    chars = list(text)
    for m in _CODE_FENCE_RE.finditer(text):
        for i in range(m.start(), m.end()):
            if chars[i] != "\n":
                chars[i] = " "
    for m in _ABBREV_RE.finditer(text):
        for i in range(m.start(), m.end()):
            if chars[i] == ".":
                chars[i] = _MASK
    return "".join(chars)


def _readable(fragment: str) -> str:
    """A fragment of a reply as the reading rules take it, with an
    abbreviation spelled out ("e.g." reads as "for example")."""
    return _ABBREV_RE.sub(lambda m: _ABBREVIATIONS[m.group(0).lower()], fragment)


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


def _clause_spans(text: str):
    """(lead, start, end, file_row) for each clause of each line outside code
    blocks, as offsets into ``text``. The lead is the line that introduces a
    list or a table, for its items (``_readable``); a row of a table that
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
        item = bool(_LIST_ITEM_RE.match(line))
        if not item:
            lead = _readable(text[at : at + len(line)]) if line.rstrip().endswith(":") else ""
        for start, end in _clause_offsets(line):
            if line[start:end].strip():
                yield (lead if item else ""), at + start, at + end, False


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


def _self_subject(before: str, *, state_passive: bool = False) -> bool:
    """True when the words ahead of a verb make it the assistant's own
    statement (see _SELF_SUBJECT_RE)."""
    before = _LIST_ITEM_RE.sub("", before, count=1)
    return bool(
        _SELF_SUBJECT_RE.match(before)
        or _EVENT_PASSIVE_RE.search(before)
        or (state_passive and _STATE_PASSIVE_RE.search(before))
    )


def _agentless(before: str) -> bool:
    """True when nothing but connectors and a list marker come before a
    verb: no "I", no passive."""
    return bool(_AGENTLESS_RE.match(_LIST_ITEM_RE.sub("", before, count=1)))


def _verb_phrase(verb: str, after: str) -> bool:
    """True when the words after an agentless verb go on like a verb phrase
    (see _VERB_PHRASE_RE)."""
    return bool(
        _VERB_PHRASE_RE.match(after)
        or (_RECIPIENT_VERB_RE.match(verb) and _NAME_NEXT_RE.match(after))
    )


def _path_claims(
    lead: str, masked: str, said: str, found: list[_Found], prev: tuple[bool, bool]
) -> list[Claim]:
    """The paths one clause claims. A document or a folder is claimed as the
    notes above _CODE_FENCE_RE say; any other file only where the assistant
    says it made, changed or removed it. A clause of paths alone continues
    the last one ("Saved to X, Y and Z")."""
    prev_claimed, prev_made = prev
    done = bool(_DONE_RE.search(said))
    made_here = _made_verb(said) and not _STATE_RE.search(said)
    earlier = bool(_EARLIER_RE.search(said))
    continuing = prev_claimed and not _PATHS_ONLY_RE.sub("", masked).strip()
    claims: list[Claim] = []
    for f in found:
        s = f.span
        ahead = masked[: s.start]
        if _OFFER_AHEAD_RE.search(f"{lead} {ahead}"):
            continue
        verb = _last_verb(ahead)
        passive = None if verb else _PASSIVE_AFTER_RE.match(masked[s.end :])
        gone = bool(verb and verb.group("gone")) or bool(
            passive and re.fullmatch(_GONE_VERBS, passive.group("verb"), re.IGNORECASE)
        )
        located = bool(_LOCATED_RE.search(_LINK_OPEN_RE.sub("", ahead)))
        source = bool(_FROM_AHEAD_RE.search(ahead))
        if source and not gone:
            # "Added the rows from `a.md`": where the work came from.
            continue
        if gone and (located or source):
            gone = False
        if f.deliverable:
            if not (s.app_link or done or located or continuing or gone or passive):
                continue
        elif verb is not None:
            if not _self_subject(ahead[: verb.start()]):
                continue
        elif not (passive or continuing):
            continue
        kind = REMOVED if gone else (FOLDER if f.folder else FILE)
        made = kind != REMOVED and (made_here or bool(passive) or (continuing and prev_made))
        claims.append(Claim(kind, s.path, made=made, earlier=earlier, deliverable=f.deliverable))
    return claims


def _act_target(kind: str, clause: str, at: int) -> str:
    """What an act names after its verb at ``at``: the branch pushed, the
    commit, the PR or issue (its address or number), where an upload went,
    who a message went to, where something was published."""
    rest = clause[at:]
    m = None
    if kind == PUSH:
        m = _BACKTICK_TOKEN_RE.search(rest) or _TO_TOKEN_RE.search(rest)
        token = (m.group(1) if m else "").strip(_TRAILING_PUNCT)
        return "" if token.lower() in _GENERIC_BRANCHES else token
    if kind == COMMIT:
        m = _SHA_RE.search(clause)
    elif kind in (PR, ISSUE, MERGE):
        m = _URL_RE.search(clause)
        if not m:
            number = _NUMBER_RE.search(clause)
            return f"#{number.group(1)}" if number else ""
    elif kind == UPLOAD:
        m = _S3_RE.search(rest) or _URL_RE.search(rest)
    elif kind == MESSAGE:
        m = _EMAIL_RE.search(rest) or _HANDLE_RE.search(rest)
        if not m:
            name = _NAME_RE.search(rest) or _NAME_NEXT_RE.match(rest)
            return name.group(name.lastindex or 0).strip() if name else ""
    elif kind == PUBLISH:
        m = _URL_RE.search(rest) or _VERSION_RE.search(rest)
    return m.group(0).rstrip(_TRAILING_PUNCT) if m else ""


def _act_claims(lead: str, clause: str, masked: str, earlier: bool) -> list[Claim]:
    """The claims one clause makes that are not paths: an artifact card, the
    acts in _KIND_PATTERNS, and a link the assistant says it made."""
    claims: list[Claim] = []
    m = _ARTIFACT_CLAIM_RE.search(masked)
    if m and not _OFFER_AHEAD_RE.search(f"{lead} {masked[: m.start()]}"):
        claims.append(Claim(ARTIFACT, made=False, earlier=earlier))
    targets: set[str] = set()
    acts = sorted(
        (m.start(), kind, m) for kind, pattern in _KIND_PATTERNS if (m := pattern.search(masked))
    )
    said_so = False
    for _at, kind, m in acts:
        before, rest = masked[: m.start()], clause[m.end() :]
        # "committed and pushed": a verb joined to one already claimed shares
        # its subject.
        joined = said_so and _JOINED_RE.search(before)
        if (
            _OFFER_AHEAD_RE.search(f"{lead} {before}")
            or _BY_OTHER_RE.search(rest)
            or not (joined or _self_subject(before, state_passive=kind in _STATE_PASSIVE_KINDS))
        ):
            continue
        if _agentless(before) and not _verb_phrase(m.group(0), masked[m.end() :]):
            continue
        if kind == MESSAGE and (
            _NOT_A_MESSAGE_RE.match(rest)
            or not (
                _RECIPIENT_VERB_RE.match(m.group(0))
                or _MESSAGE_NOUN_RE.search(rest)
                or _RECIPIENT_RE.search(rest)
            )
        ):
            continue
        target = _act_target(kind, clause, m.end())
        targets.add(target)
        claims.append(Claim(kind, target, made=True, earlier=earlier))
        said_so = True
    for u in _URL_RE.finditer(clause):
        url = u.group(0).rstrip(_TRAILING_PUNCT)
        if url in targets or url.lower().startswith("s3://"):
            continue
        before = masked[: u.start()]
        verb = None
        for v in _LINK_VERB_RE.finditer(before):
            verb = v
        if (
            verb is None
            or _OFFER_AHEAD_RE.search(f"{lead} {before}")
            or not _self_subject(before[: verb.start()])
            or (
                _agentless(before[: verb.start()])
                and not _verb_phrase(verb.group(0), before[verb.end() :])
            )
        ):
            continue
        claims.append(Claim(LINK, url, made=True, earlier=earlier))
        targets.add(url)
    return claims


def _clause_claims(
    lead: str, clause: str, prev: tuple[bool, bool]
) -> tuple[list[Claim], tuple[bool, bool]]:
    """The claims one clause of prose makes, and whether it claimed a path
    (and a made one) for a next clause of paths alone."""
    docs = _path_spans(clause)
    found = sorted(
        [_Found(s, True, False) for s in docs] + _other_paths(clause, docs),
        key=lambda f: f.span.start,
    )
    masked = _masked(clause, [f.span for f in found])
    said = f"{lead} {masked}"
    if _is_question(clause) or _NOT_MADE_RE.search(said):
        return [], (False, False)
    paths = _path_claims(lead, masked, said, found, prev) if found else []
    acts = _act_claims(lead, clause, masked, bool(_EARLIER_RE.search(said)))
    return paths + acts, (bool(paths), any(c.made for c in paths))


def _row_claims(lead: str, row: str) -> list[Claim]:
    """The claims a row of a file table makes: every path in it, unless the
    row (with the table's lead-in line) negates, offers or asks, or says its
    file moved. A row that says its file was deleted claims it is gone; a
    file that is not a document is claimed only where the row or its lead
    says what was done to it, and a document only says where it is unless
    they do."""
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
        claims += [
            Claim(REMOVED if gone else (FOLDER if f.folder else FILE), f.span.path, made=made)
            for f in others
        ]
    return claims


def read_claims(text: str) -> list[Claim]:
    """Every delivery ``text`` claims, in text order (see the module notes),
    each with the offsets of the clause that makes it."""
    text = text or ""
    out: list[Claim] = []
    prev = (False, False)
    for lead, start, end, file_row in _clause_spans(text):
        clause = _readable(text[start:end])
        if file_row:
            found = _row_claims(lead, clause)
            prev = (bool(found), any(c.made for c in found))
        else:
            found, prev = _clause_claims(lead, clause, prev)
        out += [replace(c, start=start, end=end, clause=text[start:end]) for c in found]
    return out


def asserted_paths(text: str) -> list[str]:
    """The document paths ``text`` says were made or points at where they
    are (the ``deliverable`` files of ``read_claims``), in order,
    deduplicated."""
    out: list[str] = []
    for c in read_claims(text):
        if c.kind == FILE and c.deliverable and c.target not in out:
            out.append(c.target)
    return out


def claims_an_artifact(text: str) -> bool:
    """True when ``text`` points at an artifact card as made, as opposed to
    offering one, asking about one or saying there is none."""
    return any(c.kind == ARTIFACT for c in read_claims(text))
