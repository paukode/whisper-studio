"""The claims a clause makes that are not files: an artifact card, a push, a
commit, a pull request, a merge, an issue, an upload, a message, a deploy, a
schedule, a memory, a setting, a link (read for server/goals/claims.py).

Each is its past-tense verb ("pushed", "emailed", "deployed") in the
assistant's own statement (claim_text._subject): first person, a verb with
no subject, a passive of the work ("the email has been sent", "your changes
are pushed", which says what now is), a headline naming what was acted on
("Changes pushed to `main`"). Never a third party's ("CI pushed a build",
"Dana merged it", "The PR updated the README"), a dated description ("the
commit pushed at 10:02"), how a process works ("uploaded nightly"), code
("scheduled the task with `asyncio.create_task`"), or what follows a lead
line about someone else's work ("Summary of your changes:"). Some forms say
what now is rather than what was done ("PR #12 is merged", "The fix is on
`main`", "Here's the PR: <address>", "It's live at <address>"): such a claim
has ``made`` False.

Pure functions over text; no I/O.
"""

from __future__ import annotations

import re

from server.goals.claim_text import (
    _APOS,
    _BY_OTHER_RE,
    _DATED_RE,
    _EVENT_PASSIVE_RE,
    _NAME_NEXT_RE,
    _NEGATED_AFTER_RE,
    _NOT_MADE_RE,
    _OFFER_AHEAD_RE,
    _PASSIVE_HEADLINE_AFTER_RE,
    _PENDING_PASSIVE_RE,
    _PROCESS_RE,
    _RECIPIENT_VERB_RE,
    _STATE_PASSIVE_RE,
    _URL_RE,
    _WORK_SUBJECT_RE,
    ARTIFACT,
    COMMIT,
    ISSUE,
    LINK,
    MEMORY,
    MERGE,
    MESSAGE,
    OTHERS_WORK_RE,
    PR,
    PUBLISH,
    PUSH,
    SCHEDULE,
    SETTING,
    UPLOAD,
    Claim,
    _agentless,
    _subject,
    _verb_phrase,
)

_Q = "\N{RIGHT SINGLE QUOTATION MARK}"
_ARTIFACT_CLAIM_RE = re.compile(
    r"\bartifact (?:card )?(?:above|below|attached)\b|\bartifact card\b"
    rf"|\bin (?:the|an) artifact(?:\s+(?:above|below|panel|preview|view))?\b(?!['{_Q}]s|\s+[a-z])"
    r"|\b(?:created|made|built|put|generated|added|rendered|drew)\b[^.]{0,60}?"
    r"\b(?:an?|the|your)\s+(?:[\w-]+\s+){0,2}artifact\b(?!['\N{RIGHT SINGLE QUOTATION MARK}]s|\s+(?:store|tool|api|type|folder|id)\b)"
    r"|\bhere(?:['\N{RIGHT SINGLE QUOTATION MARK}]s|\s+is)\s+(?:the|your|an?)\s+(?:[\w-]+\s+){0,2}artifact\b"
    r"|\bthe\s+artifact\s+is\s+(?:ready|up|above|below|done|open)\b"
    r"|\b(?:open|see|check|view)\s+the\s+artifact\b(?!\s+(?:store|tool|api|type|folder)\b)",
    re.IGNORECASE,
)
# Kinds this module reads that are not claims kinds of their own: a verb of
# opening (a pull request or an issue, by its object), a comment on a pull
# request (a message that needs no message noun) and "is live" (a deploy
# stated as what now is).
_OPENED = "opened"
_COMMENT = "comment"
_LIVE = "live"
_SETTING_NOT = (
    r"dialog|page|screen|panel|view|component|file|menu|tab|modal|form|schema|type|key|section"
    r"|ui|window|button|store|hook|route|sheet|module|class|object"
)
_APP_SETTINGS = (
    r"(?:dark|light)\s+mode|(?:the\s+)?theme|(?:the\s+)?default\s+(?:chat\s+)?model"
    r"|(?:the\s+)?(?:reasoning\s+)?effort|auto[- ]?(?:approve|approval|memory|compact(?:ion)?)"
    r"|(?:the\s+)?(?:ui|interface)\s+language|font\s+size"
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
    (MERGE, re.compile(r"\b(?:(?:squash|rebase)[- ])?merged\b", re.IGNORECASE)),
    (
        _OPENED,
        re.compile(r"\b(?:opened|created|raised|submitted|filed|put\s+up|made|logged)\b", re.I),
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
            r"\b(?:sent|emailed|e-mailed|messaged|posted|replied|forwarded|pinged|texted"
            rf"|notified|dm{_APOS}?e?d)\b|\b(?:has|have)\s+gone\s+out\b|\bwent\s+out\b"
            r"|\blet\s+(?:(?-i:[A-Z][a-z]+)|[\w.+-]+@[\w-]+(?:\.[\w-]+)+|the\s+team|them|him|her"
            r"|everyone)\s+know\b",
            re.IGNORECASE,
        ),
    ),
    (
        _COMMENT,
        re.compile(
            r"\bcommented\b(?=\s+(?:on|in)\b)|\bleft\s+(?:a|an|my)\s+(?:[\w-]+\s+)?(?:comment|review"
            r"|note|reply)\b|\bapproved\s+(?:the\s+)?(?:PR|pull\s+request|MR|merge\s+request|#\d+)",
            re.IGNORECASE,
        ),
    ),
    (
        PUBLISH,
        re.compile(
            r"\b(?:deployed|redeployed|published|released|shipped)\b|\brolled\s+out\b"
            r"|\bdeploy(?:ment)?\s+(?:is\s+)?(?:complete|completed|succeeded|successful|done"
            r"|finished)\b",
            re.IGNORECASE,
        ),
    ),
    (
        _LIVE,
        re.compile(rf"\b(?:is|are|{_APOS}s|{_APOS}re)\s+(?:now\s+)?live\b|\bwent\s+live\b", re.I),
    ),
    (
        SCHEDULE,
        re.compile(
            r"\bscheduled\b|\bset\s+up\s+(?:a|an|the)?\s*(?:daily|weekly|hourly|nightly|monthly"
            r"|recurring|scheduled)?\s*(?:cron(?:\s+job)?|reminder|schedule|scheduled\s+task|job)\b"
            r"|\b(?:created|added)\s+(?:a|an|the)\s+(?:new\s+)?(?:(?:daily|weekly|hourly|nightly"
            r"|monthly|recurring|scheduled)\s+)?(?:cron(?:\s+job)?|scheduled\s+task|reminder"
            r"|recurring\s+task)\b|\bset\s+(?:a|an|the)\s+(?:[\w-]+\s+)?(?:reminder|alarm|timer)\b"
            r"|\breminders?\s+(?:is\s+|are\s+)?set\b",
            re.IGNORECASE,
        ),
    ),
    (
        MEMORY,
        re.compile(
            r"\b(?:saved|stored|added|noted|recorded|written|wrote|put|kept)\b"
            r"(?=.*?\b(?:to|in|into)\s+(?:your|my|long[- ]term|the\s+session\S*|session)\s+memory\b)"
            r"|\b(?:saved|stored|added|recorded|written|wrote)\b(?=.*?\bto\s+memory\b)"
            r"|\bnoted\s+in\s+memory\b",
            re.IGNORECASE,
        ),
    ),
    (
        SETTING,
        re.compile(
            r"\b(?:set|changed|updated|switched|turned\s+(?:on|off)|enabled|disabled|toggled)\b"
            rf"(?=.*?\b(?:setting|preference)s?\b(?!\s+(?:{_SETTING_NOT})s?\b)|.*?\bfeature\s+flags?\b"
            rf"|\s+(?:on\s+|off\s+)?(?:the\s+)?(?:{_APP_SETTINGS})\b)"
            rf"|\b(?:{_APP_SETTINGS})\s+(?:is|are)\s+(?:now\s+)?(?:on|off|enabled|disabled|set\s+to)\b",
            re.IGNORECASE,
        ),
    ),
)
# Where each kind's verb says what now is, not what was done.
_STATE_FORMS = {_LIVE}
_SETTING_STATE_RE = re.compile(rf"^(?:{_APP_SETTINGS})\s+(?:is|are)\b", re.IGNORECASE)
# Between the verb and what follows: a merge needs its object ("merged the
# PR", "merged `fix-x` into main"; not "merged the two helpers into main.py");
# a dot inside a word ("v2.3", "a.md") does not end the clause.
_UNTIL_END = r"(?:[^.;]|\.(?=\w))*?"
_MERGE_OBJECT = (
    r"\b(?:PRs?|pull[- ]requests?|MRs?|merge[- ]requests?|branch(?:es)?|worktrees?)\b|#\d+"
    r"|\binto\s+[`'\"]?(?:main|master|develop|dev|trunk|release|staging|production)\b(?![\w/-]|\.\w)"
    r"|\binto\s+`(?![^`]*\.\w{1,5}`)[^`\s]+`|\binto\s+(?:origin/)?[\w.-]+/[\w./-]+"
)
_BARE = r"^[^\w\0]*$"
# What the verb must be done to, where the verb alone says too little ("I
# pushed the validation down into the model layer", "I committed the
# transaction", "I published the event on the bus" are not deliveries).
_OBJECTS = {
    PUSH: re.compile(
        rf"{_BARE}|^{_UNTIL_END}(?:\bto\s+(?:the\s+)?(?:origin|upstream|remote|github|gitlab|bitbucket"
        r"|main|master|develop|dev|trunk|release|production|prod|staging|ecr|docker\s*hub|registry"
        r"|remote|branch|repo(?:sitory)?)\b|`|\b(?:branch(?:es)?|commits?|changes|fix(?:es)?|tags?"
        r"|image|images|it|them|everything)\b)"
        r"|^\s+(?:[\w]+(?:[-/][\w.-]+)+|main|master|develop|dev|trunk)\b",
        re.IGNORECASE,
    ),
    "committed": re.compile(
        rf"{_BARE}|^{_UNTIL_END}(?:`|\b(?:changes|fix(?:es)?|files?|work|code|updates?|everything"
        r"|it|them)\b|\bto\s+(?:main|master|develop|the\s+(?:branch|repo))\b"
        r"|\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b)",
        re.IGNORECASE,
    ),
    "merged": re.compile(
        rf"{_BARE}|^{_UNTIL_END}(?:{_MERGE_OBJECT})|^\s*`(?![^`]*\.\w{{1,5}}`)[^`\s]+`"
        r"|^\s+(?:[\w]+(?:[-/][\w.-]+)+)\b",
        re.IGNORECASE,
    ),
    "published": re.compile(
        rf"{_BARE}|^{_UNTIL_END}(?:\b(?:packages?|versions?|releases?|site|page|docs?|documentation"
        r"|app|build|image|article|post|blog|extension|crate|gem|module|library|changelog|notes)\b"
        r"|\bv?\d+\.\d+|\bto\s+(?:npm|pypi|production|prod|staging|github|the\s+app\s+store"
        r"|vercel|netlify|crates\.io|rubygems|the\s+marketplace)\b|https?://|\0|`)",
        re.IGNORECASE,
    ),
    "deployed": re.compile(
        rf"{_BARE}|^{_UNTIL_END}(?:\b(?:app|site|service|stack|function|lambda|api|build|changes"
        r"|fix(?:es)?|release|version|it|them|everything|infrastructure|worker|bot|image|backend"
        r"|frontend|update|page|manifest)\b|\bto\s+\w+|https?://|\0|`)",
        re.IGNORECASE,
    ),
    "scheduled": re.compile(
        rf"{_BARE}|^{_UNTIL_END}\b(?:tasks?|jobs?|runs?|reminders?|cron|checks?|reports?|agents?"
        r"|workflows?|it|this|them|digests?|backups?|emails?)\b",
        re.IGNORECASE,
    ),
}
_VERB_OBJECTS = {
    "released": "published",
    "shipped": "deployed",
    "redeployed": "deployed",
    "rolled": "deployed",
    "squash-merged": "merged",
    "rebase-merged": "merged",
}
_HEADLINE_NOUNS = {
    PUSH: r"changes|commits?|branch(?:es)?|fix(?:es)?|code|tags?|images?|updates?",
    COMMIT: r"changes|fix(?:es)?|files?|updates?",
    MERGE: r"PRs?|pull\s+requests?|branch(?:es)?|MRs?|#\d+|`[^`]+`",
    PR: r"PR|pull\s+request|MR|merge\s+request",
    ISSUE: r"issues?|tickets?",
    UPLOAD: r"files?|exports?|reports?|data|backups?|artifacts?|images?|uploads?",
    MESSAGE: r"e-?mails?|messages?|summar(?:y|ies)|notes?|invites?|invitations?|repl(?:y|ies)"
    r"|updates?|reports?|notifications?|reminders?|recaps?|digests?",
    PUBLISH: r"sites?|apps?|packages?|releases?|versions?|docs|builds?|images?|pages?",
    SCHEDULE: r"tasks?|jobs?|reminders?|runs?|checks?|reports?",
}
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
# Between a link verb and its address: a reference ("created the helper
# following https://docs...") makes the address a source, not the delivery.
_LINK_REFERENCE_RE = re.compile(
    r"\b(?:following|per|from|see|using|based\s+on|like|as\s+in|described|according\s+to|via"
    r"|docs?|documentation|reference|guide|example|pattern)\b",
    re.IGNORECASE,
)
_BACKTICK_TOKEN_RE = re.compile(r"`([^`\s]+)`")
_TO_TOKEN_RE = re.compile(r"\b(?:to|into|on)\s+(?:the\s+)?[`'\"]?([\w./-]+)[`'\"]?", re.IGNORECASE)
_BARE_BRANCH_RE = re.compile(
    r"^\s+(?!the\b|a\b|an\b|it\b|them\b)([\w]+(?:[-/][\w.-]+)+|main|master|develop|dev|trunk)\b"
)
_GENERIC_BRANCHES = frozenset(
    {"remote", "origin", "github", "gitlab", "upstream", "repo", "repository", "the", "it",
     "branch", "server", "your", "my", "a", "an", "registry"}
)  # fmt: skip
# A commit id: seven or more hex digits with a letter and a digit among them
# ("1500000 rows" and "20260929" are not).
_SHA = r"(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}"
_SHA_RE = re.compile(rf"\b{_SHA}\b")
_NUMBER_RE = re.compile(r"(?:#|\b(?:PR|MR|issue|pull request)\s+#?)(\d+)\b", re.IGNORECASE)
_S3_RE = re.compile(r"\bs3://[^\s`'\"()<>\[\]]+")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_HANDLE_RE = re.compile(r"(?<![\w.])(?:@[\w.-]*\w|#(?!\d+\b)[\w.-]*\w)")
_NAME_RE = re.compile(
    r"\b(?:to|let)\s+(?:(?:Dr|Mr|Mrs|Ms|Prof)\.?\s+)?([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)"
)
_VERSION_RE = re.compile(r"\bv?\d+\.\d+(?:\.\d+)*\b")
_TRAILING_PUNCT = ".,;:!?)]}>'\"`*"
_JOINED_RE = re.compile(r"(?:\band|&|,)\s*(?:then\s+|also\s+)?$", re.IGNORECASE)
_PR_HEADLINE_RE = re.compile(
    r"^[\s\W]*(?:the\s+)?(?P<noun>PR|pull\s+request|MR|merge\s+request|issue)(?:\s+#\d+)?"
    r"\s+(?:opened|created|raised|submitted|filed|is\s+up|up)\b",
    re.IGNORECASE,
)
# "Here's the PR: <address>", "PR: #7", "The PR is <address>", "Issue #40
# is open".
_PR_STATE_RE = re.compile(
    rf"^[\s\W]*(?:here(?:{_APOS}s|\s+is|\s+are)\s+)?(?:the\s+|your\s+|a\s+)?(?:new\s+)?"
    r"(?P<noun>PRs?|pull\s+requests?|MRs?|merge\s+requests?|issues?)\b(?:\s+#\d+)?\s*"
    r"(?:is\s+(?:now\s+)?(?:up|open|ready|here|live)\b|is\s+(?=https?://|\0|#\d)|:)",
    re.IGNORECASE,
)
_PR_NOUN_RE = re.compile(r"\b(?:PRs?|pull[- ]requests?|MRs?|merge[- ]requests?)\b", re.I)
_ISSUE_NOUN_RE = re.compile(r"\b(?:issues?|tickets?|bug\s+reports?)\b", re.I)
_OBJECT_NEAR_RE = r"^(?:\s+(?:a|an|the|new|draft|my|your|this|that|one|two|\d+|[\w-]+)){0,3}?\s+"
# A pull request or issue opened by its address, or by number alone.
_PR_URL_RE = re.compile(r"/(?:pull|merge_requests)/\d+")
_ISSUE_URL_RE = re.compile(r"/issues/\d+")
# "The fix is now on `main`", "Changes are on origin/main": a push (or a
# merge) stated as what now is.
_ON_BRANCH_RE = re.compile(
    rf"\b(?:is|are|{_APOS}s|{_APOS}re)\s+(?:(?:now|all|also)\s+)*(?:up\s+)?on\s+(?:the\s+)?"
    r"(?P<b>`[^`\s]+`|origin(?:/[\w./-]+)?\b|upstream(?:/[\w./-]+)?\b"
    r"|(?:main|master|develop|trunk)\b(?![\w/-]|\.\w))",
    re.IGNORECASE,
)
_WORK_NOUN_END_RE = re.compile(
    r"\b(?:fix(?:es)?|changes?|branch(?:es)?|commits?|code|work|everything|it|that|this|them"
    rf"|all\s+of\s+it|the\s+pr|`[^`]+`|{_SHA})\W*$",
    re.IGNORECASE,
)
_COMMIT_STATE_RE = re.compile(
    rf"\bthe\s+commit\s+is\s+`?(?P<a>{_SHA})`?|\bcommit\s+`?(?P<b>{_SHA})`?\s+is\s+(?:now\s+)?(?:on|in)\b",
    re.IGNORECASE,
)
# "The file is in the bucket now: s3://...": an upload stated as what now is.
_UPLOAD_STATE_RE = re.compile(
    rf"\b(?:is|are|{_APOS}s)\s+(?:now\s+)?(?:up\s+)?(?:in|on)\s+(?:the\s+|your\s+)?(?:s3\s+)?bucket\b",
    re.IGNORECASE,
)
# "It's live at <address>": the thing now live, not who made it so.
_STATE_SUBJECT_RE = re.compile(
    r"^[\s\W]*(?:(?:done|ok|great|all\s+set|good\s+news)\b[\s\W]*)*"
    r"(?:(?:the|your|my|our|this|that|it|everything|all)\b[\w\s.-]{0,40}?)?\s*$",
    re.IGNORECASE,
)
# Code, not a delivery: "scheduled the cleanup with `asyncio.create_task`",
# "set the retry setting in `server/config.py`", "the preferences handler".
_CODE_CONTEXT_RE = re.compile(
    r"`[^`\s]*[._(/][^`\s]*`|\b(?:function|method|class|handler|callback|coroutine|thread"
    r"|asyncio|queue|event\s+loop|decorator|hook|variable|constant|endpoint|middleware"
    r"|env(?:ironment)?\s+var(?:iable)?s?|parameter|argument|default\s+value|in\s+the\s+code)\b",
    re.IGNORECASE,
)


def _object_target(clause: str, before_at: int) -> str:
    """A push's or a merge's branch named ahead of a passive verb ("Branch
    `fix-x` has been pushed")."""
    found = _BACKTICK_TOKEN_RE.findall(clause[:before_at])
    return found[-1].strip(_TRAILING_PUNCT) if found else ""


def _act_target(kind: str, clause: str, at: int) -> str:
    """What an act names after its verb at ``at``: the branch pushed, the
    commit, the PR or issue (its address or number), where an upload went,
    who a message went to, where something was published."""
    rest = clause[at:]
    m = None
    if kind == PUSH:
        bare = _BARE_BRANCH_RE.match(rest)
        if bare:
            return bare.group(1).strip(_TRAILING_PUNCT)
        for pattern in (_TO_TOKEN_RE, _BACKTICK_TOKEN_RE):
            m = pattern.search(rest)
            token = (m.group(1) if m else "").strip(_TRAILING_PUNCT)
            if token and token.lower() not in _GENERIC_BRANCHES:
                return token
        return ""
    if kind == COMMIT:
        m = _SHA_RE.search(clause)
    elif kind in (PR, ISSUE, MERGE):
        m = _URL_RE.search(rest) or _URL_RE.search(clause)
        if not m:
            number = _NUMBER_RE.search(rest) or _NUMBER_RE.search(clause)
            if number:
                return f"#{number.group(1)}"
            if kind == MERGE:
                branch = _BACKTICK_TOKEN_RE.match(rest.lstrip()) or _BARE_BRANCH_RE.match(rest)
                if branch and not re.search(r"\.\w{1,5}$", branch.group(1)):
                    return branch.group(1).strip(_TRAILING_PUNCT)
            return ""
    elif kind == UPLOAD:
        m = _S3_RE.search(rest) or _URL_RE.search(rest)
    elif kind == MESSAGE:
        m = _EMAIL_RE.search(rest) or _HANDLE_RE.search(rest)
        if not m:
            name = _NAME_RE.search(rest) or _NAME_NEXT_RE.match(rest)
            if name:
                return name.group(1).strip()
            number = _NUMBER_RE.search(rest)
            return f"#{number.group(1)}" if number else ""
    elif kind == PUBLISH:
        m = _URL_RE.search(rest) or _VERSION_RE.search(rest)
    return m.group(0).rstrip(_TRAILING_PUNCT) if m else ""


def _opened_kind(clause: str, masked: str, m: re.Match, passive: bool) -> str | None:
    """What a verb of opening opened: a pull request or an issue named near
    it (ahead of a passive), by its address or by number, or None."""
    after, before = masked[m.end() :], masked[: m.start()]
    for noun, kind in ((_PR_NOUN_RE, PR), (_ISSUE_NOUN_RE, ISSUE)):
        near = re.match(_OBJECT_NEAR_RE + noun.pattern, after, re.IGNORECASE)
        if near or re.match(r"^\s+" + noun.pattern, after, re.IGNORECASE):
            return kind
        if passive and noun.search(before):
            return kind
    url = _URL_RE.match(clause[m.end() :].lstrip()) or re.match(
        r"^\s*(?:(?:a|an|the|new)\s+)*(?:\S+\s+)?(?:at|:)\s*(\S+)", clause[m.end() :]
    )
    address = url.group(0) if url else ""
    if _PR_URL_RE.search(address):
        return PR
    if _ISSUE_URL_RE.search(address):
        return ISSUE
    if re.match(r"^\s+#\d+\b", after):
        return PR
    return None


def act_claims(lead: str, clause: str, masked: str) -> list[Claim]:
    """The claims one clause makes that are not paths (see the module
    notes)."""
    claims: list[Claim] = []
    others = bool(OTHERS_WORK_RE.search(lead))
    dated = bool(_DATED_RE.search(masked))
    code = bool(_CODE_CONTEXT_RE.search(clause))
    m = _ARTIFACT_CLAIM_RE.search(masked)
    if (
        m
        and not _OFFER_AHEAD_RE.search(f"{lead} {masked[: m.start()]}")
        and not _NOT_MADE_RE.search(f"{lead} {masked}")
    ):
        claims.append(Claim(ARTIFACT, made=False))
    process = bool(_PROCESS_RE.search(masked))
    targets: set[str] = set()
    headline = _PR_HEADLINE_RE.match(masked)
    if headline:
        kind = ISSUE if headline.group("noun").lower() == "issue" else PR
        target = _act_target(kind, clause, headline.end())
        targets.add(target)
        claims.append(Claim(kind, target, made=True, firm=not others))
    state = None if headline else _PR_STATE_RE.match(masked)
    if state and not _NOT_MADE_RE.search(f"{lead} {masked}"):
        kind = ISSUE if state.group("noun").lower().startswith("issue") else PR
        target = _act_target(kind, clause, state.end())
        if target:
            targets.add(target)
            claims.append(Claim(kind, target, made=False, firm=not others))
    acts = sorted(
        (m.start(), kind, m) for kind, pattern in _KIND_PATTERNS for m in pattern.finditer(masked)
    )
    said_so: Claim | None = None
    said_who = ""
    short = len(masked.split()) <= 14
    for _at, kind, m in acts:
        if (headline or state) and kind == _OPENED:
            continue
        before, rest, after = masked[: m.start()], clause[m.end() :], masked[m.end() :]
        if (
            _OFFER_AHEAD_RE.search(f"{lead} {before}")
            or _NOT_MADE_RE.search(f"{lead} {before}")
            or _NEGATED_AFTER_RE.match(after)
            or _BY_OTHER_RE.search(rest)
        ):
            continue
        # "committed and pushed": a verb joined to one already claimed shares
        # its subject.
        if said_so is not None and _JOINED_RE.search(before):
            who = said_who
        else:
            who = _subject(before)
        if kind == _LIVE:
            # "The site is live at <address>": the thing now live.
            who = "state" if _STATE_SUBJECT_RE.match(before) else None
        if kind == SETTING and _SETTING_STATE_RE.match(masked[m.start() :]):
            # "Dark mode is now on": the setting as it now is.
            who = "state" if _agentless(before) else None
        if who is None:
            continue
        if process and who in ("soft", "headline", "state"):
            # How a process works ("the export is uploaded nightly").
            continue
        if dated and who != "firm":
            # A dated description ("the commit pushed to main at 10:02").
            continue
        if kind in (SCHEDULE, SETTING) and code:
            continue
        nouns_kind = PR if kind == _OPENED else kind
        if who == "headline":
            nouns = _HEADLINE_NOUNS.get(nouns_kind)
            if not (
                short and nouns and re.search(rf"(?<![\w-])(?:{nouns})\W*$", before, re.IGNORECASE)
            ):
                continue
            if not _PASSIVE_HEADLINE_AFTER_RE.match(after):
                # "The PR merged the fix": the noun did it.
                continue
        if (
            who == "firm"
            and kind != SETTING
            and _agentless(before)
            and not _verb_phrase(m.group(0), after)
        ):
            continue
        # A passive names what it acted on ahead of the verb ("All changes are
        # committed").
        passive = bool(
            _WORK_SUBJECT_RE.search(before)
            or _EVENT_PASSIVE_RE.search(before)
            or _STATE_PASSIVE_RE.search(before)
        )
        if kind == _OPENED:
            opened = _opened_kind(clause, masked, m, passive)
            if opened is None:
                continue
            kind = opened
        first = m.group(0).lower().split()[0]
        first = _VERB_OBJECTS.get(first, first)
        wanted = _OBJECTS.get(kind) if kind == PUSH else _OBJECTS.get(first)
        if wanted is not None and not (
            wanted.match(before)
            if passive and not after.strip(" .,;:!?*_")
            else wanted.match(after) or (passive and wanted.match(before))
        ):
            # A bare verb ends its clause ("Committed."); a bare passive
            # ("after that lock is released") names nothing delivered.
            continue
        if passive and _PENDING_PASSIVE_RE.search(before):
            # "when they are posted": what will happen, not what did.
            continue
        if (
            kind == MESSAGE
            and not re.match(r"(?:has|have|went|let)\b", m.group(0), re.I)
            and (
                _NOT_A_MESSAGE_RE.match(rest)
                or not (
                    _RECIPIENT_VERB_RE.match(m.group(0))
                    or _MESSAGE_NOUN_RE.search(rest)
                    or _MESSAGE_NOUN_RE.search(before)
                    or _RECIPIENT_RE.search(rest)
                )
            )
        ):
            continue
        real = MESSAGE if kind == _COMMENT else PUBLISH if kind == _LIVE else kind
        target = _act_target(real, clause, m.end())
        if real == MESSAGE and m.group(0).lower().startswith("let "):
            target = m.group(0)[4:-5].strip()
        if not target and passive and real in (PUSH, MERGE):
            target = _object_target(clause, m.start())
        targets.add(target)
        made = who != "state" and kind not in _STATE_FORMS
        said_so = Claim(real, target, made=made, firm=who != "soft" and not others)
        said_who = who
        claims.append(said_so)
    for m in _ON_BRANCH_RE.finditer(masked):
        before = masked[: m.start()]
        if (
            not _WORK_NOUN_END_RE.search(before)
            or _NOT_MADE_RE.search(f"{lead} {before}")
            or _OFFER_AHEAD_RE.search(f"{lead} {before}")
            or (dated and not _subject(before) == "firm")
        ):
            continue
        branch = m.group("b").strip("`").removeprefix("origin/").removeprefix("upstream/")
        target = "" if branch.lower() in ("origin", "upstream") else branch
        claims.append(Claim(PUSH, target, made=False, firm=not others))
    for m in _UPLOAD_STATE_RE.finditer(masked):
        found = _S3_RE.search(clause)
        if found and not _NOT_MADE_RE.search(f"{lead} {masked[: m.start()]}"):
            target = found.group(0).rstrip(_TRAILING_PUNCT)
            claims.append(Claim(UPLOAD, target, made=False, firm=not others))
    for m in _COMMIT_STATE_RE.finditer(masked):
        if not _NOT_MADE_RE.search(f"{lead} {masked[: m.start()]}"):
            claims.append(Claim(COMMIT, m.group("a") or m.group("b"), made=False, firm=not others))
    for u in _URL_RE.finditer(clause):
        url = u.group(0).rstrip(_TRAILING_PUNCT)
        if url in targets or url.lower().startswith("s3://"):
            continue
        before = masked[: u.start()]
        verb = None
        for v in _LINK_VERB_RE.finditer(before):
            verb = v
        if verb is None:
            continue
        between = before[verb.end() :]
        who = _subject(before[: verb.start()])
        if (
            who not in ("firm", "soft", "state")
            or len(between) > 60
            or _LINK_REFERENCE_RE.search(between)
            or _OFFER_AHEAD_RE.search(f"{lead} {before}")
            or _NOT_MADE_RE.search(f"{lead} {before}")
            or (
                who == "firm"
                and _agentless(before[: verb.start()])
                and not _verb_phrase(verb.group(0), between)
            )
        ):
            continue
        claims.append(Claim(LINK, url, made=True, firm=who in ("firm", "state") and not others))
        targets.add(url)
    return claims
