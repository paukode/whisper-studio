"""The words the claim reader (server/goals/claims.py) reads a reply by: the
kinds of claim, whose statement a verb makes, what negates, offers or points
back, where a sentence ends, and which lines are code, quoted or a draft.
Shared by the reading of paths (claim_paths.py) and of acts (claim_acts.py),
and by the stream guard (server/chat/claim_guard.py), which splits a reply
into sentences and code the same way.

Pure functions over text; no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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
    offsets in the text read, and ``clause`` its words. ``firm`` is False
    where the reader cannot tell the claim from a description (a passive, a
    row of a table of changes): it is checked only where the turn tried what
    it says (server/goals/evidence.py)."""

    kind: str
    target: str = ""
    made: bool = True
    earlier: bool = False
    deliverable: bool = False
    firm: bool = True
    start: int = 0
    end: int = 0
    clause: str = ""


_ABBREVIATIONS = {"e.g.": "for example", "i.e.": "that is"}
_ABBREV_RE = re.compile(r"\b(?:e\.g|i\.e)\.", re.IGNORECASE)
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+")

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

_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s<>()\[\]`\"']+", re.IGNORECASE)
_MASK = "\0"

_APOS = "['\N{RIGHT SINGLE QUOTATION MARK}]"

# The verbs that say the assistant made, changed or removed a file, and the
# words that say it only is somewhere ("is saved at", "is in").
_MADE_VERBS = (
    r"saved|wrote|written|rewrote|rewritten|created|exported|generated|produced|stored|placed"
    r"|put|rendered|built|converted|updated|downloaded|attached|copied|moved|renamed|edited"
    r"|modified|changed|fixed|added|refactored|patched|replaced|appended|inserted|restored"
    r"|drafted|compiled|made"
)
_GONE_VERBS = r"deleted|removed|trashed|erased|untracked|stopped\s+tracking"
_VERB_RE = re.compile(rf"\b(?:(?P<gone>{_GONE_VERBS})|(?P<made>{_MADE_VERBS}))\b", re.IGNORECASE)
_STATE_RE = re.compile(
    rf"\b(?:is|are|{_APOS}s)\s+(?:(?:now|still|also|already)\s+)*"
    r"(?:saved|stored|located|kept|placed|available|ready|in|at|under|inside)\b",
    re.IGNORECASE,
)
# A clause that points back to an earlier turn (points_back). "Already" does
# not: "I've already pushed it" is as often this turn's work.
_EARLIER_RE = re.compile(
    r"\b(?:earlier|previously|yesterday|as before|before (?:this|now)|so far|ago"
    r"|last (?:time|turn|session|week|night|month|year)|as (?:i|we) (?:mentioned|said|noted)"
    r"|(?:earlier )?in this (?:session|conversation|chat)"
    r"|(?:in|from|during) (?:the|a|an|my|our|your) (?:previous|earlier|last|prior) \w+)\b",
    re.IGNORECASE,
)

# Whose statement it is. The assistant's own: connectors ("Done:", "Also"),
# then "I" or "we" with helpers ("I've also"), or nothing ("Pushed to main");
# or anything that ends in "I" or "we" ("After the tests passed I pushed"),
# unless "I" opens a subordinate clause ("When I pushed, it failed").
_CONNECTORS = (
    r"(?:and|also|then|finally|done|ok|okay|great|now|just|so|next|lastly|first|second|third"
    r"|plus|all set|as requested|as asked|good news|update|result|summary|status)"
)
_HELPERS = (
    r"(?:\s+(?:have|had|also|just|now|then|already|successfully|finally|quickly"
    r"|went ahead and|was able to|were able to|managed to|did))*"
)
_SELF_SUBJECT_RE = re.compile(
    rf"^[\s\W]*(?:{_CONNECTORS}\b[\s\W]*)*(?:(?:i|we)(?:{_APOS}(?:ve|d|m|re))?{_HELPERS}\s*)?$",
    re.IGNORECASE,
)
_SELF_END_RE = re.compile(rf"\b(?:i|we)(?:{_APOS}(?:ve|d))?{_HELPERS}\s*$", re.IGNORECASE)
_SUBORDINATE_RE = re.compile(
    r"\b(?:when|while|as|once|if|until|unless|whenever|because|since|though|although|before)"
    r"\s+(?:i|we)\b",
    re.IGNORECASE,
)
# A subject and an event passive ("the branch was pushed", "it has been
# sent"): in a reply, the assistant reporting its own act (_passive_kind: "had
# been" and "gets" only describe).
_EVENT_PASSIVE_RE = re.compile(
    rf"(?:\b(?:was|were|has been|have been|had been|got|gets|is now|are now)"
    rf"|{_APOS}s(?:\s+now)?\s+been)(?:\s+(?:now|also|just|successfully|already|finally|all))*\s*$",
    re.IGNORECASE,
)
# A subject and a present passive ("all changes are committed and pushed"):
# what now is (Claim.made False). How something works ("the image is pushed
# to ECR nightly", "when CI passes, it is deployed") claims nothing.
_STATE_PASSIVE_RE = re.compile(
    rf"(?:\b(?:is|are)|{_APOS}s|{_APOS}re)(?:\s+(?:now|also|just|all|already))*\s*$",
    re.IGNORECASE,
)
# A passive whose subject is the work itself ("All changes are committed and
# pushed", "the fix has been pushed", "branch `x` is merged") reports the
# assistant's own act, and is checked as such.
_WORK_SUBJECT_RE = re.compile(
    r"(?:^|[\s\W])(?:all\s+(?:of\s+)?(?:the\s+|my\s+|your\s+|these\s+)?changes|(?:the|my|your"
    r"|these|those)\s+(?:changes|commits?|fix(?:es)?|work)|everything)"
    r"\s+(?:is|are|was|were|has\s+been|have\s+been)(?:\s+(?:now|also|all|already|both))*\s*$",
    re.IGNORECASE,
)
# A sentence that reports what a source says ("According to the log, the
# fix was pushed at 10:02") claims nothing of the assistant's own: its claims
# are checked only where the turn tried the act.
_REPORTED_RE = re.compile(
    r"\baccording\s+to\b|\bper\s+the\b|\b(?:the|this|that)\s+(?:log|logs|output|history|ci"
    r"|build|pipeline|report|audit\s+log|changelog)\s+(?:shows?|says?|states?|lists?)\b",
    re.IGNORECASE,
)
# A passive in a clause about what will happen ("when they are posted",
# "once it is merged").
_PENDING_PASSIVE_RE = re.compile(
    r"\b(?:when|once|if|until|after|before|as\s+soon\s+as|unless)\s+\S+(?:\s+\S+){0,3}\s+"
    r"(?:is|are|was|were|has\s+been|have\s+been|gets?)\s*$",
    re.IGNORECASE,
)
# A headline: a short phrase naming what was acted on, then the verb
# ("Changes pushed to `main`", "Email sent to Dana"). The noun must be one the
# act is done to, so "CI pushed a build" and "Dana merged it" are not.
_HEADLINE_RE = re.compile(
    r"^[\s\W]*(?:(?:the|your|all|my|both)\s+)?[A-Za-z][\w-]*"
    r"(?:\s+(?:[A-Za-z][\w-]*|#\d+|`[^`\s]+`)){0,2}[\s*_`]*$"
)
_NOT_HEADLINE_RE = re.compile(
    r"\b(?:i|we|you|he|she|they|it|this|that|these|those|there|is|are|was|were|be|been|has"
    r"|have|had|will|would|should|can|could|may|might|must|do|does|did|not|no|someone"
    r"|somebody|nobody|everyone|anyone|anybody)\b",
    re.IGNORECASE,
)
# How a process works, not what the assistant did ("uploaded nightly").
_PROCESS_RE = re.compile(
    r"\b(?:nightly|daily|hourly|weekly|monthly|every|each\s+time|whenever|automatically"
    r"|periodically|on\s+(?:each|every|failure|success|merge|push))\b",
    re.IGNORECASE,
)
# A sentence about what happens when something else does (see
# _FIRST_PERSON_RE).
_CONDITIONAL_RE = re.compile(
    r"^[\s\W]*(?:(?:when|once|after|whenever|each\s+time|every\s+time|if|as\s+soon\s+as|until"
    r"|unless|in\s+case)\s+(?!(?:i|we)\b)|on\s+(?:failure|success|error|merge|push|release"
    r"|deploy)\b)",
    re.IGNORECASE,
)
# A third party did it: "merged by Dana", "pushed by the CI", "uploaded by
# the release script".
_BY_OTHER_RE = re.compile(
    r"\bby\s+(?:@\w|[A-Z][\w-]*|(?i:the\s+(?:[\w-]+\s+)?(?:user|team|ci|bot|pipeline|workflow"
    r"|action|scheduler|reviewer|maintainer|script|job|build|process|system|tool|hook)s?\b"
    r"|ci\b|someone\b|another\b))"
)
_NEGATED_AFTER_RE = re.compile(r"^[\s*_`]*(?:nothing|none|no\b|not\b)", re.IGNORECASE)

# An act with no subject ("Pushed to main") reads as the assistant's only
# where its verb goes on like a verb phrase: into an object, a place, a
# target or nothing. "Scheduled Python checks" and "Merged PRs:" are names of
# things, not acts. A message verb may take its recipient ("Emailed Dana").
_AGENTLESS_RE = re.compile(rf"^[\s\W]*(?:{_CONNECTORS}\b[\s\W]*)*$", re.IGNORECASE)
_VERB_PHRASE_RE = re.compile(
    r"^[\s*_]*(?:$|[.,;:!?)\]`'\"(\[#@~/\N{BULLET}\0]|[^\w\s]|https?://|\d|v\d"
    r"|(?:the|a|an|it|them|this|that|these|those|your|my|our|his|her|their|all|both|each"
    r"|every|one|two|three|to|into|in|on|at|from|with|for|over|back|up|out|off|and|then|as"
    r"|successfully|PR|MR|pull request|merge request|issue|ticket)\b)",
    re.IGNORECASE,
)
_RECIPIENT_VERB_RE = re.compile(
    rf"^(?:emailed|e-mailed|messaged|pinged|texted|notified|dm{_APOS}?e?d)$", re.IGNORECASE
)
_NAME_NEXT_RE = re.compile(r"^[\s*_]*([A-Z][a-z]+)\b")


def _is_question(clause: str) -> bool:
    return clause.rstrip(" \t\"')]*_`").endswith("?")


_SENTENCE_BREAK_RE = re.compile(
    r"[.!?][)\]\"'*_`]*\s+"
    r"|[\N{IDEOGRAPHIC FULL STOP}\N{FULLWIDTH EXCLAMATION MARK}\N{FULLWIDTH QUESTION MARK}]\s*|\n"
)


# After a headline's verb ("Changes pushed to main"): a place, a manner or
# nothing. A direct object ("The CI job pushed the image") makes the noun the
# one who did it.
_PASSIVE_HEADLINE_AFTER_RE = re.compile(
    r"^[\s*_`'\"]*(?:$|[.,;:!?)\]\0]|(?:to|into|in|on|at|for|from|with|as|via|onto|under|over"
    r"|through|across|and|then|successfully|already|now|just|by)\b)",
    re.IGNORECASE,
)

# A dated description: a clock time or how long ago ("the commit pushed to
# main at 10:02 broke it", "was merged two days ago").
_DATED_RE = re.compile(
    r"\bat\s+\d{1,2}:\d{2}\b|\b\d+\s+(?:seconds?|minutes?|mins?|hours?|hrs?|days?|weeks?"
    r"|months?|years?)\s+ago\b|\bago\b|\byesterday\b|\blast\s+(?:night|week|month|year)\b",
    re.IGNORECASE,
)

# A lead line about someone else's work: its items describe it.
OTHERS_WORK_RE = re.compile(
    rf"\b(?:your|their|his|her|(?-i:(?!(?:Here|That|It|What|There|Let|Who)\b)[A-Z][a-z]+){_APOS}s)"
    r"\s+(?:[\w-]+\s+)?(?:changes?|commits?|work"
    r"|edits?|updates?|branch|pr|pull\s+request|diff)\b"
    r"|\bhere(?:'s|\s+is)\s+what\s+(?:changed|(?:the|this|that)\s+(?:pr|commit|diff|patch|branch))"
    r"|\bchanges\s+(?:in|from|made\s+by)\s+(?:the|this|that|your|their)\b"
    r"|\bthe\s+(?:pr|diff|patch|commit|branch)\s+(?:changes|does|adds|removes|updates|touches)\b",
    re.IGNORECASE,
)
# A lead line in which the assistant says what it did to the files listed
# under it ("I updated these files:", "Deleted:"): each item is a claim.
_LEAD_MADE_RE = re.compile(
    rf"^[\s\W]*(?:{_CONNECTORS}\b[\s\W]*)*(?:(?:i|we)(?:{_APOS}(?:ve|d))?{_HELPERS}\s+)?"
    rf"(?:(?P<gone>{_GONE_VERBS})|{_MADE_VERBS})\b[^:]*:\s*$",
    re.IGNORECASE,
)


# ── Where code, quotes and drafts are ─────────────────────────────────────
# A fenced code block (CommonMark): a line that opens with three or more
# backticks or tildes, after up to three spaces, a list marker or a quote
# marker; a backtick fence's info string holds no backtick ("```npm test```
# runs it" opens none). It closes at a line of the same character, at least
# as long, and nothing else; unclosed, it runs to the end.
_FENCE_OPEN_RE = re.compile(
    r"^(?:[ \t]*(?:[-*+\N{BULLET}]|\d+[.)])[ \t]+|[ \t]*>[ \t]?)*[ \t]*"
    r"(?P<fence>`{3,}|~{3,})(?P<info>.*)$"
)
_FENCE_CLOSE_RE = re.compile(r"^(?:[ \t]*>)*[ \t]*(?P<fence>`{3,}|~{3,})[ \t]*$")
# A line quoted from elsewhere ("> Hi team, ...") and the text of a draft the
# reply hands over ("Here's a draft you can send:", "Suggested commit
# message:") are not the assistant's own statements. A draft runs until a
# heading, a rule, a paragraph in which the assistant speaks again ("I",
# "Let me know", "Want me to"), or the end of the code block it opened with.
_QUOTE_RE = re.compile(r"^[ \t]{0,3}>")
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}\s")
_RULE_RE = re.compile(r"^[ \t]{0,3}(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$")
_TEXT_ITEM = (
    r"(?:drafts?|templates?|suggestions?|samples?|examples?|commit\s+message|pr\s+description"
    r"|pull\s+request\s+description|release\s+notes|changelog(?:\s+entry)?|standup(?:\s+update)?"
    r"|status\s+update|announcement|tweet|slack\s+(?:message|post)|email\s+(?:text|body)"
    r"|message\s+(?:text|body))"
)
_DRAFT_LEAD_RE = re.compile(
    rf"^[\s\W]*(?:(?:here(?:{_APOS}s|\s+is|\s+are)\s+)?(?:a|an|the|your|my|one|some)?\s*"
    rf"(?:[\w-]+\s+){{0,2}}{_TEXT_ITEM}\b|(?:suggested|proposed|draft|example|sample|for\s+example)\b"
    rf"|here(?:{_APOS}s|\s+is)\s+(?:a|an|the|your)\s+(?:[\w-]+\s+){{0,2}}(?:message|email|reply"
    r"|note|post|update|summary)\s+(?:you\s+can|to\s+send|to\s+post)\b)",
    re.IGNORECASE,
)
_SPEAKS_AGAIN_RE = re.compile(
    rf"^[\s*_]*(?:(?:i|we)(?:{_APOS}\w+)?\b|let\s+me\b|want\s+me\b|would\s+you\b|should\s+i\b"
    r"|shall\s+i\b|if\s+you\b|do\s+you\b|anything\s+else\b|feel\s+free\b|happy\s+to\b)",
    re.IGNORECASE,
)


def fence_open(line: str) -> tuple[str, int] | None:
    """The fence a line opens (its character and length), or None."""
    m = _FENCE_OPEN_RE.match(line)
    if not m or (m.group("fence")[0] == "`" and "`" in m.group("info")):
        return None
    return m.group("fence")[0], len(m.group("fence"))


def fence_closes(line: str, fence: tuple[str, int]) -> bool:
    m = _FENCE_CLOSE_RE.match(line)
    return bool(m) and m.group("fence")[0] == fence[0] and len(m.group("fence")) >= fence[1]


class Lines:
    """Reads a reply line by line (see the notes above): which lines are
    code, quoted, or part of a draft the reply hands over. The claim reader
    and the stream guard read a reply the same way through it."""

    def __init__(self) -> None:
        self.fence: tuple[str, int] | None = None
        self.draft = False
        # A code block that opened in a draft is the draft ("Example:" and
        # its block): the draft ends where the block does.
        self.draft_block = False
        self.after_blank = False

    def kind(self, line: str) -> str:
        """What ``line`` is ("code", "quote", "draft" or "prose"), given the
        lines before it; it does not advance."""
        if self.fence is not None or fence_open(line):
            return "code"
        if _QUOTE_RE.match(line):
            return "quote"
        if self.draft and not (
            self.after_blank
            and (_SPEAKS_AGAIN_RE.match(line) or _HEADING_RE.match(line) or _RULE_RE.match(line))
        ):
            return "draft"
        return "prose"

    def feed(self, line: str) -> str:
        """Read one whole line and return what it is."""
        kind = self.kind(line)
        if self.fence is not None:
            if fence_closes(line, self.fence):
                self.fence = None
                if self.draft_block:
                    self.draft = self.draft_block = False
        elif kind == "code":
            self.fence = fence_open(line)
            self.draft_block = self.draft
        if kind == "prose":
            self.draft = line.rstrip().endswith(":") and bool(_DRAFT_LEAD_RE.search(line))
        self.after_blank = not line.strip()
        return kind


# ── Sentences ──────────────────────────────────────────────────────────────
# A dot that ends no sentence: "e.g." and "i.e." (read as "for example" and
# "that is"), a title before a name ("Dr. Smith"), and "etc." and the like
# where the next word starts in lower case ("the docs, etc. and more").
_TITLE_RE = re.compile(r"\b(?:Dr|Mr|Mrs|Ms|Prof|St|Jr|Sr|Mt)\.(?=\s+[A-Z])")
_SOFT_ABBREV_RE = re.compile(
    r"\b(?:etc|vs|cf|approx|incl|esp|fig|eq|no)\.(?=\s+[a-z])", re.IGNORECASE
)


def _reading_copy(text: str) -> str:
    """``text`` at the same length, with each line of code, quote or draft
    blanked (``Lines``) and each abbreviation's dot hidden, so its lines and
    clauses keep the offsets of ``text`` and "e.g." ends no sentence."""
    chars = list(text)
    lines = Lines()
    at = 0
    for line in text.split("\n"):
        if lines.feed(line) != "prose":
            chars[at : at + len(line)] = " " * len(line)
        at += len(line) + 1
    for pattern in (_ABBREV_RE, _TITLE_RE, _SOFT_ABBREV_RE):
        for m in pattern.finditer(text):
            for i in range(m.start(), m.end()):
                if chars[i] == ".":
                    chars[i] = _MASK
    return "".join(chars)


def _readable(fragment: str) -> str:
    """A fragment of a reply as the reading rules take it, with an
    abbreviation spelled out ("e.g." reads as "for example")."""
    return _ABBREV_RE.sub(lambda m: _ABBREVIATIONS[m.group(0).lower()], fragment)


# ── Whose statement it is ─────────────────────────────────────────────────
# "I fixed the bug and pushed the change": a verb joined to a first-person
# (or agentless) clause of the assistant's shares its subject.
_PAST_VERB = (
    r"(?:\w+ed|ran|wrote|rewrote|built|rebuilt|made|did|redid|undid|took|got|put|set|found|kept"
    r"|left|brought|sent|spent|split|drew|began|chose|gave|held|led|met|paid|read|said|saw"
    r"|sold|told|thought|went|fed|shut|cut|hit|let|dug|spun|swept|struck|bound|wound|laid"
    r"|lent|dealt|meant|sped|withdrew)"
)
_COORDINATED_RE = re.compile(
    rf"^[\s\W]*(?:{_CONNECTORS}\b[\s\W]*)*(?:(?:i|we)(?:{_APOS}(?:ve|d))?{_HELPERS}\s+)?"
    rf"{_PAST_VERB}\b(?:[^.;:!?]|\.(?=\w))*?(?:\band|&|,)\s*(?:then\s+|also\s+|finally\s+"
    r"|later\s+)?$",
    re.IGNORECASE,
)
# The auxiliary of a passive: a perfect ("has been sent") or a past ("was
# sent") reports an act, "is"/"are" says what now is, "had been" and "gets"
# describe.
_PERFECT_RE = re.compile(
    rf"(?:\b(?:has|have)\s+been|{_APOS}s\s+been)(?:\s+(?:now|also|just|all|already|both"
    r"|successfully|finally))*\s*$",
    re.IGNORECASE,
)
_PAST_PASSIVE_RE = re.compile(
    r"\b(?:was|were|got)(?:\s+(?:now|also|just|successfully|already|finally|all|both))*\s*$",
    re.IGNORECASE,
)
_SOFT_PASSIVE_RE = re.compile(
    r"\b(?:had\s+been|gets?)(?:\s+(?:now|also|just|all|already|both))*\s*$", re.IGNORECASE
)


def _passive_kind(before: str) -> str:
    if _SOFT_PASSIVE_RE.search(before):
        return "soft"
    if _PERFECT_RE.search(before) or _PAST_PASSIVE_RE.search(before):
        return "firm"
    return "state"


def _subject(before: str, *, passive: bool = True) -> str | None:
    """Whose statement the verb after ``before`` makes: "firm" for the
    assistant's own (first person, no subject, a clause joined to one of
    those, a perfect or past passive), "state" for a present passive (what
    now is: "your changes are pushed"), "headline" for a short phrase naming
    what was acted on, "soft" for a description, or None. With ``passive``
    False every passive is "soft": a path after a passive verb is where or
    what else it names, not its subject ("`a.py` was updated in `b.md`")."""
    before = _LIST_ITEM_RE.sub("", before, count=1)
    if _SELF_SUBJECT_RE.match(before) or _COORDINATED_RE.match(before):
        return "firm"
    if _SELF_END_RE.search(before) and not _SUBORDINATE_RE.search(before):
        return "firm"
    if (
        _WORK_SUBJECT_RE.search(before)
        or _EVENT_PASSIVE_RE.search(before)
        or _STATE_PASSIVE_RE.search(before)
    ):
        return _passive_kind(before) if passive else "soft"
    if _HEADLINE_RE.match(before) and not _NOT_HEADLINE_RE.search(before):
        return "headline"
    return None


def _agentless(before: str) -> bool:
    """True when nothing but connectors and a list marker come before a
    verb: no "I", no passive."""
    return bool(_AGENTLESS_RE.match(_LIST_ITEM_RE.sub("", before, count=1)))


# An agentless verb may also take a bare name for its object: a branch
# ("Pushed fix-x to origin", "Pushed main"), a file, an address, or who a
# message went to ("Emailed bob the notes", "Sent Dana the notes").
_BARE_OBJECT_RE = re.compile(
    r"^[\s*_]*(?:[\w.+-]+@[\w-]+(?:\.[\w-]+)+|\w+(?:[-/][\w.-]+)+|[\w-]+\.[A-Za-z0-9]{1,10}\b"
    r"|(?:main|master|develop|dev|trunk|staging|production|prod)\b)"
)
_SEND_NAME_RE = re.compile(r"^[\s*_]*[A-Z][a-z]+\s+(?:the|a|an|her|his|their|my|your|our)\b")
_SENT_VERB_RE = re.compile(r"^(?:sent|forwarded)$", re.IGNORECASE)


def _verb_phrase(verb: str, after: str) -> bool:
    """True when the words after an agentless verb go on like a verb phrase
    (see _VERB_PHRASE_RE and _BARE_OBJECT_RE)."""
    if _VERB_PHRASE_RE.match(after) or _BARE_OBJECT_RE.match(after):
        return True
    if _RECIPIENT_VERB_RE.match(verb):
        return bool(_NAME_NEXT_RE.match(after) or re.match(r"^\s+\w+\s+the\b", after))
    return bool(_SENT_VERB_RE.match(verb) and _SEND_NAME_RE.match(after))


# A sentence about what happens when something else happens ("When CI
# passes, the image is pushed to ECR", "On failure, an alert is sent") claims
# nothing, unless the assistant speaks in the clause that claims ("After the
# tests passed, I pushed the fix").
_FIRST_PERSON_RE = re.compile(rf"\b(?:i|we)(?:{_APOS}(?:ve|d|m|re|ll))?\b", re.IGNORECASE)
# An earlier-turn word that belongs to someone else's act ("the crash you
# reported earlier") says nothing of when the claim's act happened.
_OTHER_SUBJECT_AHEAD_RE = re.compile(
    r"\b(?:you|they|he|she|someone|the\s+user)\s+(?:[\w-]+\s+){0,3}$", re.IGNORECASE
)


# A sentence that opens by pointing back ("As I mentioned earlier, I pushed
# the fix") points back for every clause of it.
_HEAD_POINTS_BACK_RE = re.compile(
    r"^[\s\W]*(?:as\s+(?:i|we)\s+(?:mentioned|said|noted)|as\s+before|like\s+i\s+said|earlier"
    r"|previously|before\s+(?:this|now)|yesterday|so\s+far|last\s+(?:time|turn|session|week|night)"
    r"|(?:in|during)\s+(?:the|my|our|a)\s+(?:previous|earlier|last|prior)\s+\w+)\b",
    re.IGNORECASE,
)


def points_back(lead: str, clause: str, head: str = "") -> bool:
    """True when the claim's own clause points back to an earlier turn
    (see _EARLIER_RE), or the line introducing its list does, or the
    sentence opens by doing so (``head``: the sentence ahead of the clause).
    A word that belongs to someone else's act ("the crash you reported
    earlier") does not."""
    if _EARLIER_RE.search(lead) or (head and _HEAD_POINTS_BACK_RE.match(head)):
        return True
    return any(
        not _OTHER_SUBJECT_AHEAD_RE.search(clause[: m.start()])
        for m in _EARLIER_RE.finditer(clause)
    )
