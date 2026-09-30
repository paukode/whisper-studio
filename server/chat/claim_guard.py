"""Hold back a sentence that claims a delivery until the claim is verified.

The model streams its reply as it writes it, so a sentence saying a file was
saved, a branch pushed or a message sent reached the user before anything
checked it, and in real sessions such a sentence was false. The guard sits
between the provider's text and the SSE text frames (runner.py). Text flows
through at once, except that the sentence in progress is held while it could
still become a claim (it names a path or an address, or has a verb the
reader knows: claims.TRIGGER_RE), and any other text trails the stream by a
few words, so a claim that shows up early in its sentence is held from the
sentence's start. When a sentence ends, or the model turns to a tool call,
server/goals/claims.py reads it and server/goals/evidence.py checks each claim
it makes:

- no claim, or every claim holds: the sentence is released, and a
  ``deliveries`` frame lists what was verified (the chat shows chips);
- a claim does not hold: the sentence is not shown, nor what follows it. A
  round that calls tools holds its tool calls' frames behind it too, until
  the round's calls are known: a sentence none of them could make true is
  dropped then, and the rest (tool cards included) released in order; one a
  call could make true waits for the calls to run, with the tool cards sent
  ahead of it, and comes after them as its own paragraph. In a round with no
  calls the sentence is dropped at the round's end. The completion gate then
  sends the model back (deliverables.check_claims). A sentence part of which
  was already shown ends in "...".

Code, quoted text and a draft the reply hands over are not the assistant's
own statements and flow through unread (claim_text.Lines), as the reader
skips them. A cut at the output limit is no sentence end: the sentence runs
on in the continuation (a retried continuation too), and is read whole.

When the turn ends (at the end of its last round where no gate can send the
model back: subagents, scheduled and headless runs), each dropped claim that
still does not hold is stated in the server's own words ("Not saved:
`~/a.docx` does not exist."), unless a later sentence corrected it, so a
false claim is never shown as fact. A turn that stops for an approval hands
its start time, its dropped claims and everything from its first held
sentence on to the continuation, which checks them against the calls that ran
meanwhile and shows them first, in order.

The guard keeps every call of the turn as it first read it, so compaction or
a salvage round cannot make a done act look undone (or a failed one done),
and the completion gate reads the same calls. Every consumer of the runner's
frames (the chat, subagents, scheduled and headless runs, voice) gets the
guarded text, and so does the model's next turn, which is rebuilt from what
the chat showed; the memory hooks read the turn without the dropped
sentences (``redact``). The memory agents' notes are not replies and are not
guarded.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field, replace

from server.chat.engine.events import (
    Frame,
    Heartbeat,
    RoundError,
    RoundResult,
    TextDelta,
)
from server.goals import claims as c
from server.goals.call_results import turn_calls
from server.goals.claim_text import Lines
from server.goals.claims import _paths_only
from server.goals.evidence import (
    Before,
    Call,
    Evidence,
    Verdict,
    before_of,
    check,
    could_make,
    mentions_as_correction,
    not_done,
)
from server.utils import ndjson_dumps

log = logging.getLogger("whisper-studio")

# Text with no sign of a claim trails the stream by this many characters, so
# a sentence whose claim appears within its first words is held whole.
_LOOKBACK = 120
# A block is read back at most this far for the lines that introduce it (a
# list's lead line, a table's header); further back, the lead line is kept.
_BLOCK_BACK = 4000
# Where a sentence ends: a line break, end punctuation (and a closing quote
# or bracket) before whitespace, a full-width end, or a period run straight
# into the next sentence ("...q3.docx.The tests pass"). After an abbreviation
# it ends only where the next word does not start in lower case ("etc. The
# tests", "etc. `pytest` passes"); after a title never ("Dr. Smith").
_UNIT_BREAK_RE = re.compile(
    r"\n|[.!?]+[)\]\"'*_`]*(?=\s)"
    r"|[\N{IDEOGRAPHIC FULL STOP}\N{FULLWIDTH EXCLAMATION MARK}\N{FULLWIDTH QUESTION MARK}]+"
    r"|(?<=[a-z0-9)\]`'\"*_])[.!?](?=[A-Z][a-z]+\s)"
)
_ABBREV_END_RE = re.compile(
    r"\b(?:(?P<never>e\.g|i\.e|dr|mr|mrs|ms|prof|st|jr|sr|mt)|etc|vs|cf|approx|no|inc|ltd|fig"
    r"|eq|incl|esp)\.$",
    re.IGNORECASE,
)
_NEXT_WORD_RE = re.compile(r"\s+(\S)")
_LEAD_RE = re.compile(r"^(?!\s*(?:[-*\N{BULLET}]|\d+[.)])\s).*:\s*$")
_ITEM_RE = re.compile(r"^\s*(?:[-*\N{BULLET}]|\d+[.)])\s")
_TABLE_RULE_RE = re.compile(r"\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*")
# A continuation of a paused turn keeps the turn's start, its dropped claims
# and the text it held: scope -> _Paused. Unclaimed entries expire.
_PAUSED_STARTS: dict[str, _Paused] = {}
_PAUSE_TTL_S = 3600.0


@dataclass
class _Held:
    """A sentence whose claims did not hold when it ended, the receipts of
    those of its claims that did, and whether part of it was shown."""

    start: int
    end: int
    failures: list[tuple[c.Claim, Verdict]]
    receipts: list[dict] = field(default_factory=list)
    partial: bool = False
    text: str = ""


@dataclass
class _Dropped:
    """A claim the user was not shown, and how much text the turn had shown
    by then (a later sentence that corrects it follows)."""

    claim: c.Claim
    verdict: Verdict
    shown_at: int


@dataclass
class _Paused:
    """What a turn paused for an approval hands its continuation: its start,
    its prompt (which the continuation must share), its dropped claims, and
    the text from its first held sentence on with the sentences held in it
    (offsets into that text) and the parts already dropped."""

    started_at: float
    prompt: str
    dropped: list[_Dropped]
    text: str = ""
    held: list[_Held] = field(default_factory=list)
    drops: list[tuple[int, int, str]] = field(default_factory=list)
    stamp: float = field(default_factory=time.monotonic)


def _checking_claims() -> bool:
    try:
        from server.infrastructure.feature_flags import is_enabled

        return is_enabled("deliverable_check")
    except Exception:  # noqa: BLE001 - an unreadable flag keeps the check on
        return True


def _calls_tools(content) -> bool:
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") in ("tool_use", "function_call") for b in content
    )


def _sse(items: list) -> list[str]:
    out: list[str] = []
    for item in items:
        if isinstance(item, TextDelta):
            out.append(f"data: {ndjson_dumps({'text': item.text})}\n\n")
        elif isinstance(item, Frame):
            out.append(f"data: {ndjson_dumps(item.payload)}\n\n")
    return out


def _drop_span(buf: str, h: _Held) -> tuple[int, int, str]:
    """Where a dropped sentence is cut from ``buf``: the space ahead of it
    stays, the space after it goes with it, and a line of its own takes its
    line break too; one part of which was shown leaves "..."."""
    start, end = h.start, h.end
    while start < end and buf[start] in " \t":
        start += 1
    while end < len(buf) and buf[end] in " \t":
        end += 1
    if start == 0 or buf[start - 1] == "\n":
        if end < len(buf) and buf[end] == "\n" and not h.text.endswith("\n"):
            end += 1
    return start, end, "... " if h.partial else ""


def _visible_text(buf: str, drops: list[tuple[int, int, str]], start: int, end: int) -> str:
    parts: list[str] = []
    at = start
    for s, e, instead in sorted(drops):
        if e <= at or s >= end:
            continue
        if s > at:
            parts.append(buf[at:s])
        parts.append(instead)
        at = max(at, e)
    if at < end:
        parts.append(buf[at:end])
    return "".join(parts)


def _expire_paused() -> None:
    now = time.monotonic()
    for scope in [s for s, p in _PAUSED_STARTS.items() if now - p.stamp > _PAUSE_TTL_S]:
        _PAUSED_STARTS.pop(scope, None)


class ClaimGuard:
    """One turn's guard (see the module notes). The runner passes each
    round's provider events through ``wrap``, calls ``after_tools`` once the
    round's calls have run, reads ``ledger`` for the gate, sends what
    ``finish`` returns before the turn's [DONE], and hands the memory hooks
    ``redact(messages)``."""

    def __init__(
        self,
        *,
        workspace: str | None = None,
        plan_mode: bool = False,
        session_id: str = "",
        enabled: bool = True,
        started_at: float | None = None,
        notes_at_round_end: bool = False,
        receipts: list | None = None,
        paused: _Paused | None = None,
    ):
        self.workspace = workspace
        self.plan_mode = plan_mode
        self.session_id = session_id
        self.enabled = enabled
        self.started_at = time.time() if started_at is None else started_at
        # No gate can send the model back (subagents, scheduled and headless
        # runs): the notes close the last round, where the run's report is.
        self.notes_at_round_end = notes_at_round_end
        # The deliveries earlier replies verified (the chat's history).
        self.receipts = list(receipts or [])
        self._messages: list = []
        self._calls: dict[str, Call] = {}
        self._text_cache: dict = {}
        self._before: Before | None = None
        self._ev = Evidence()
        # Whether the session holds an artifact card from an earlier turn:
        # looked up once, off the event loop, the first time a card claim
        # does not hold.
        self._cards: bool | None = None
        self._wants_cards = False
        self._shown = ""
        self._dropped: list[_Dropped] = list(paused.dropped) if paused else []
        self._dropped_texts: list[str] = []
        self._announced: set[tuple[str, str]] = set()
        self._paused_in = paused if paused and paused.text else None
        self._carry = False
        self._retry = False
        self._round_done = False
        # A finished round's result while the guard sends the text it held
        # ahead of it: a Stop there must still record the round's cost.
        self._holding: RoundResult | None = None
        self._new_round()

    @classmethod
    def for_turn(cls, ctx) -> ClaimGuard:
        """The guard for a runner turn: off for the memory agents, whose
        notes are not replies, and with the ``deliverable_check`` flag, which
        also turns off the completion gate's claim check; a continuation of a
        paused turn keeps its start, its dropped claims and its held text."""
        from server.goals.deliverables import last_user_prompt

        _expire_paused()
        scope = ctx.turn_scope_id or ctx.session_id
        paused = _PAUSED_STARTS.pop(scope, None)
        if paused is not None and paused.prompt != last_user_prompt(ctx.messages):
            paused = None
        policy = getattr(ctx, "policy", None)
        return cls(
            workspace=ctx.ws_path or None,
            plan_mode=ctx.plan_mode,
            session_id=scope,
            enabled=ctx.cost_source != "memory" and _checking_claims(),
            started_at=paused.started_at if paused else None,
            notes_at_round_end=policy is not None and not policy.completion_gate,
            receipts=getattr(ctx, "earlier_deliveries", None),
            paused=paused,
        )

    def _new_round(self) -> None:
        self._buf = ""
        self._out = 0
        self._judged = 0
        self._lines = Lines()
        self._line = (-1, "prose")
        self._para = 0
        # The paragraph's list lead line and table header, as (offset, text).
        self._lead = (-1, "")
        self._table = (-1, "")
        self._last_line = (-1, "")
        self._prose_lines: list[tuple[int, bool]] = [(0, True)]
        self._held: list[_Held] = []
        self._drops: list[tuple[int, int, str]] = []
        self._seen: set[tuple[int, str, str]] = set()
        self._receipts: list[tuple[int, dict]] = []
        self._frames: list[tuple[int, object]] = []
        self._scanned = 0
        self._scan_at = 0
        self._hot = False
        self._late = False

    def _evidence(self) -> Evidence:
        if self._before is None:
            self._before = before_of(self._messages, self.receipts, self._text_cache)
        return Evidence.of(
            self._messages,
            workspace=self.workspace,
            started_at=self.started_at,
            session_has_artifact=bool(self._cards),
            plan_mode=self.plan_mode,
            kept=self._calls,
            before=self._before,
            cache=self._text_cache,
        )

    def ledger(self) -> list[Call]:
        """Every call of the turn the guard has seen, for the gate."""
        return list(self._calls.values())

    # ── the runner's calls ───────────────────────────────────────────────

    async def wrap(self, events, messages: list):
        """One round's provider events, with its text held as the module
        notes say. A round attempt that fails after it said something shows
        what it said before its error, as it did unguarded (that text counts
        as streamed, so the round is not run again over it); one that failed
        before saying anything leaves the guard as it was, for its retry."""
        if not self.enabled:
            async for ev in events:
                yield ev
            return
        for item in self._begin_round(messages):
            yield item
        fed_at = len(self._buf)
        frames_at = len(self._frames)
        try:
            async for ev in events:
                if isinstance(ev, TextDelta):
                    for item in self._feed(ev.text):
                        yield item
                    if self._wants_cards and self._cards is None:
                        await self._load_cards()
                        for item in self._settle(final=False):
                            yield item
                    continue
                if isinstance(ev, RoundResult):
                    self._holding = ev
                    if self._wants_cards and self._cards is None:
                        await self._load_cards()
                    for item in self._end_of(ev):
                        yield item
                    yield ev
                    self._holding = None
                    continue
                if isinstance(ev, RoundError):
                    if ev.retryable and len(self._buf) == fed_at:
                        # Nothing said yet: a retry starts from here.
                        self._frames = self._frames[:frames_at]
                        self._retry = True
                    else:
                        for item in self._end_round(final=True, last=False):
                            yield item
                    yield ev
                    continue
                if isinstance(ev, Heartbeat):
                    yield ev
                    continue
                if isinstance(ev, Frame):
                    yield ev
                    continue
                # A tool call, thinking or the output cap: the text before it
                # is complete. Behind a held sentence it waits its turn.
                for item in self._close_tail() + self._release(len(self._buf)):
                    yield item
                if self._held or self._frames:
                    self._frames.append((len(self._buf), ev))
                else:
                    yield ev
        except Exception:
            if len(self._buf) == fed_at:
                self._frames = self._frames[:frames_at]
                self._retry = True
            else:
                for item in self._end_round(final=True, last=False):
                    yield item
            raise

    def held_usage(self):
        """The usage of a round that finished while its held text was still
        being sent (the adapter already handed its cost over), else None."""
        return self._holding.usage if self._holding is not None else None

    def after_tools(self, messages: list) -> list[str]:
        """The round's calls have run: read them into the ledger (before
        compaction can shorten their results), check the round's held
        sentences again, drop those that still do not hold and release the
        rest, a paragraph of its own after the tool cards."""
        if not self.enabled:
            return []
        self._messages = messages
        self._ev = self._evidence()
        items = self._settle(final=True)
        if self._late:
            self._late = False
            if any(isinstance(i, TextDelta) for i in items) and not self._shown.endswith(
                ("\n", " ")
            ):
                self._shown += "\n\n"
                items.append(TextDelta(text="\n\n"))
        return _sse(items)

    def finish(self, *, paused: bool = False) -> list[str]:
        """The frames to send before the turn's [DONE]: the text still held,
        and a note for each dropped claim that still does not hold. A turn
        that paused for an approval hands its held text on instead."""
        if not self.enabled:
            return []
        items = self._close_tail()
        if paused:
            from server.goals.deliverables import last_user_prompt

            items += self._settle(final=False)
            hand = _Paused(self.started_at, last_user_prompt(self._messages), list(self._dropped))
            if self._held:
                first = self._held[0].start
                items += self._release(first)
                hand.text = self._buf[first:]
                hand.held = [
                    replace(h, start=h.start - first, end=h.end - first) for h in self._held
                ]
                hand.drops = [(s - first, e - first, i) for s, e, i in self._drops if s >= first]
                self._held = []
            else:
                items += self._release(len(self._buf))
            self._frames = []
            _expire_paused()
            _PAUSED_STARTS[self.session_id] = hand
            self._dropped = []
            return _sse(items)
        items += self._settle(final=True)
        return _sse(items + self._note_items())

    def redact(self, messages: list) -> list:
        """``messages`` without the sentences the user was not shown, for
        readers of the turn that are not the model (the memory hooks): a copy,
        since the model's own next rounds keep what it wrote."""
        if not self._dropped_texts:
            return messages
        from server.goals.deliverables import turn_messages

        turn = turn_messages(messages)
        out = list(messages[: len(messages) - len(turn)])
        for m in turn:
            if isinstance(m, dict) and m.get("role") == "assistant":
                out.append({**m, "content": self.redact_content(m.get("content"))})
            else:
                out.append(m)
        return out

    def redact_content(self, content):
        """One assistant message's content without the sentences the user
        was not shown."""
        drops = [t.strip() for t in self._dropped_texts if t.strip()]
        if not drops:
            return content
        if isinstance(content, str):
            return _cut(content, drops)
        if not isinstance(content, list):
            return content
        return [
            {**b, "text": _cut(str(b.get("text", "")), drops)}
            if isinstance(b, dict) and b.get("type") == "text"
            else b
            for b in content
        ]

    # ── reading the stream ───────────────────────────────────────────────

    def _begin_round(self, messages: list) -> list:
        items: list = []
        if self._carry or self._retry:
            # The continuation of a cut, or a retry of an attempt that said
            # nothing: the sentence in progress goes on.
            self._carry = self._retry = False
        elif self._round_done:
            # A finished round whose calls never reported back.
            items = self._settle(final=True)
            self._new_round()
        else:
            self._new_round()
        self._round_done = False
        self._holding = None
        self._messages = messages
        self._ev = self._evidence()
        return items + self._resume_paused()

    def _resume_paused(self) -> list:
        """What a paused turn held, in order, ahead of the continuation's
        own text: each held sentence shown if a call that ran meanwhile made
        it true, else dropped and noted at the end like any other."""
        paused, self._paused_in = self._paused_in, None
        if paused is None:
            return []
        drops = list(paused.drops)
        receipts: list[dict] = []
        for h in paused.held:
            failures = []
            for claim, _was in h.failures:
                verdict = check(claim, self._ev)
                if verdict.ok:
                    if verdict.receipt:
                        receipts.append(verdict.receipt)
                else:
                    failures.append((claim, verdict))
            receipts += h.receipts
            if failures:
                drops.append(_drop_span(paused.text, h))
                self._dropped += [_Dropped(cl, v, len(self._shown)) for cl, v in failures]
                self._dropped_texts.append(h.text)
        text = _visible_text(paused.text, drops, 0, len(paused.text))
        items: list = []
        if text.strip():
            if not text.endswith(("\n", " ")):
                text += "\n\n"
            self._shown += text
            items.append(TextDelta(text=text))
        return items + self._announce(receipts)

    def _feed(self, text: str) -> list:
        self._buf += text
        items: list = []
        while (end := self._unit_end(self._judged)) is not None:
            items += self._close_unit(self._judged, end)
            self._judged = end
        return items + self._release_tail()

    def _unit_end(self, start: int) -> int | None:
        at = max(start, self._scan_at - 8)
        while m := _UNIT_BREAK_RE.search(self._buf, at):
            if m.group(0) == "\n" or not m.group(0).startswith((".", "!", "?")):
                return self._unit_found(m.end())
            abbrev = _ABBREV_END_RE.search(self._buf[max(0, m.start() - 7) : m.start() + 1])
            if not abbrev:
                return self._unit_found(m.end())
            if not abbrev.group("never"):
                nxt = _NEXT_WORD_RE.match(self._buf, m.end())
                if nxt is None:
                    self._scan_at = m.start()
                    return None
                if not nxt.group(1).islower():
                    return self._unit_found(m.end())
            at = m.end()
        self._scan_at = len(self._buf)
        return None

    def _unit_found(self, end: int) -> int:
        self._scan_at = end
        return end

    def _block_start(self, at: int) -> tuple[int, str]:
        """Where the block holding offset ``at`` starts (the paragraph, at
        most _BLOCK_BACK back, from a line of prose), and the lead line to
        read ahead of it when the window leaves it out."""
        start = self._para
        if at - start > _BLOCK_BACK:
            start = next(
                (s for s, prose in self._prose_lines if s >= at - _BLOCK_BACK and prose), at
            )
        kept = [text for at_, text in (self._lead, self._table) if 0 <= at_ < start]
        return start, "\n".join(kept)

    def _close_unit(self, start: int, end: int) -> list:
        line_at = self._buf.rfind("\n", 0, start) + 1
        if self._line[0] != line_at:
            # The first sentence of a line: what the line is (code, a quote,
            # a draft, prose) follows from the lines before it.
            self._line = (line_at, self._lines.kind(self._buf[line_at:end].rstrip("\n")))
        prose = self._line[1] == "prose"
        if end > 0 and self._buf[end - 1] == "\n":
            self._note_line(line_at, self._buf[line_at : end - 1])
        self._scanned, self._hot = end, False
        if not prose:
            return self._release(end)
        receipts, failures = self._judge(start, end)
        if failures:
            text_start = max(start, self._out)
            self._held.append(
                _Held(
                    text_start,
                    end,
                    failures,
                    receipts,
                    partial=self._out > start,
                    text=self._buf[text_start:end],
                )
            )
            return self._release(end)
        self._receipts += [(end, r) for r in receipts]
        return self._release(end)

    def _note_line(self, at: int, line: str) -> None:
        """A whole line: advance the reading of lines (code, quotes, drafts)
        and keep where the paragraph starts, its list lead line and table
        header, for ``_block_start``. The lead line stays across a blank line
        and under its items, as the reader keeps it."""
        kind = self._lines.feed(line)
        nxt = at + len(line) + 1
        prose = kind == "prose"
        self._prose_lines.append((nxt, prose))
        if not line.strip():
            if prose:
                self._para = nxt
        elif prose:
            item = bool(_ITEM_RE.match(line)) or (self._lead[0] >= 0 and _paths_only(line))
            if _LEAD_RE.match(line):
                self._lead = (at, line)
            elif not item and "|" not in line:
                self._lead = (-1, "")
            if "|" in line and _TABLE_RULE_RE.fullmatch(line):
                head_at, head = self._last_line
                if "|" in head:
                    self._table = (head_at, f"{head}\n{line}")
            elif "|" not in line:
                self._table = (-1, "")
        self._last_line = (at, line)
        self._line = (-1, "prose")
        if len(self._prose_lines) > 4000:
            self._prose_lines = self._prose_lines[-2000:]

    def _close_tail(self) -> list:
        """The sentence in progress, read as ended (the round ended or the
        model turned to a tool)."""
        if self._judged >= len(self._buf):
            return []
        items = self._close_unit(self._judged, len(self._buf))
        self._judged = len(self._buf)
        self._scan_at = len(self._buf)
        return items

    def _judge(self, start: int, end: int) -> tuple[list[dict], list[tuple[c.Claim, Verdict]]]:
        """Check the claims of the sentence [start, end), reading its block
        from its start so a list item keeps its lead line."""
        block, lead = self._block_start(start)
        prefix = f"{lead}\n" if lead else ""
        receipts: list[dict] = []
        failures: list[tuple[c.Claim, Verdict]] = []
        for claim in c.read_claims(prefix + self._buf[block:end]):
            s = block + claim.start - len(prefix)
            e = block + claim.end - len(prefix)
            key = (s, claim.kind, claim.target)
            if e <= start or s >= end or key in self._seen:
                continue
            self._seen.add(key)
            verdict = check(claim, self._ev)
            if verdict.ok:
                if verdict.receipt:
                    receipts.append(verdict.receipt)
                continue
            if claim.kind == c.ARTIFACT and self._cards is None:
                self._wants_cards = True
            failures.append((replace(claim, start=s, end=e), verdict))
        return receipts, failures

    def _release_tail(self) -> list:
        """Release what the sentence in progress cannot make a claim of."""
        if self._held:
            return []
        line_at = self._buf.rfind("\n") + 1
        if self._lines.kind(self._buf[line_at:]) != "prose":
            # Code, a quote or a draft: no claim of the assistant's own.
            return self._release(len(self._buf))
        if not self._hot:
            # Only the text new since the last look, and a word's worth
            # before it (a word split between two deltas).
            from_at = max(self._judged, self._scanned - 30)
            self._hot = bool(c.TRIGGER_RE.search(self._buf, from_at))
            self._scanned = len(self._buf)
        if self._hot:
            return []
        return self._release(max(self._judged, len(self._buf) - _LOOKBACK))

    def _release(self, limit: int) -> list:
        """Send the text up to ``limit`` (never past a held sentence), less
        the dropped sentences, with each frame held behind it at its place,
        then the receipts of what was sent."""
        stop = min([h.start for h in self._held] + [limit])
        items: list = []
        while True:
            due = self._frames[0][0] if self._frames else None
            upto = stop if due is None or due > stop else due
            if upto > self._out:
                text = _visible_text(self._buf, self._drops, self._out, upto)
                self._out = upto
                if text:
                    self._shown += text
                    items.append(TextDelta(text=text))
            if due is not None and due <= stop and self._out >= due:
                items.append(self._frames.pop(0)[1])
                continue
            break
        ready = [r for at, r in self._receipts if at <= self._out]
        self._receipts = [(at, r) for at, r in self._receipts if at > self._out]
        return items + self._announce(ready)

    def _announce(self, receipts: list[dict]) -> list:
        fresh = []
        for r in receipts:
            key = (r.get("kind", ""), r.get("target", ""))
            if key not in self._announced:
                self._announced.add(key)
                fresh.append(r)
        return [Frame({"deliveries": {"items": fresh}})] if fresh else []

    # ── settling what was held ───────────────────────────────────────────

    def _end_of(self, ev: RoundResult) -> list:
        """A round's result: a cut goes on in the next round; a round that
        calls tools drops what none of its calls could make true and sends
        its tool cards; any other ends the round."""
        if ev.stop_reason in ("max_tokens", "pause_turn"):
            # Not a sentence end: the continuation completes it.
            self._carry = True
            items = self._release(self._judged)
            items += [e for _at, e in self._frames]
            self._frames = []
            return items
        if _calls_tools(ev.content):
            items = self._close_tail()
            pending = turn_calls([{"role": "assistant", "content": ev.content}])
            items += self._settle(final=True, only=lambda h: not self._reachable(h, pending))
            items += self._release(len(self._buf))
            if self._frames:
                # A held sentence a call may make true: the calls' cards go
                # first, and the sentence follows them once they ran.
                self._late = True
                items += [e for _at, e in self._frames]
                self._frames = []
            self._round_done = True
            return items
        return self._end_round(final=True, last=True)

    def _reachable(self, h: _Held, pending: list[Call]) -> bool:
        return any(could_make(claim, pending, self._ev) for claim, _v in h.failures)

    def _end_round(self, *, final: bool, last: bool) -> list:
        items = self._close_tail()
        self._round_done = True
        if not final:
            return items + self._release(len(self._buf))
        items += self._settle(final=True)
        items += [e for _at, e in self._frames]
        self._frames = []
        if last and self.notes_at_round_end:
            # Where no gate can send the model back, the notes belong to the
            # run's last round: its report.
            items += self._note_items()
        return items

    def _settle(self, *, final: bool, only=None) -> list:
        """Check the held sentences again (a call may have made one true):
        release those that hold, and with ``final`` drop the others (those
        ``only`` picks, when given) and release everything after them."""
        if self._held:
            self._ev = self._evidence()
        still: list[_Held] = []
        for h in self._held:
            failures: list[tuple[c.Claim, Verdict]] = []
            for claim, _was in h.failures:
                verdict = check(claim, self._ev)
                if verdict.ok:
                    if verdict.receipt:
                        h.receipts.append(verdict.receipt)
                else:
                    failures.append((claim, verdict))
            if not failures:
                self._receipts += [(h.end, r) for r in h.receipts]
                continue
            h = replace(h, failures=failures)
            if not final or (only is not None and not only(h)):
                still.append(h)
                continue
            self._drops.append(_drop_span(self._buf, h))
            self._receipts += [(h.end, r) for r in h.receipts]
            self._dropped += [_Dropped(cl, v, len(self._shown)) for cl, v in failures]
            self._dropped_texts.append(h.text)
            log.info(
                "claim guard: held back %d claim(s) that did not hold: %s",
                len(failures),
                "; ".join(v.note for _cl, v in failures),
            )
        self._held = still
        return self._release(len(self._buf) if final else self._judged)

    async def _load_cards(self) -> None:
        self._wants_cards = False
        if not self.session_id:
            self._cards = False
            return
        from server.artifacts import list_artifacts

        try:
            self._cards = bool(await asyncio.to_thread(list_artifacts, self.session_id))
        except Exception as e:  # noqa: BLE001 - a lookup bug must never abort a turn
            log.warning("artifact lookup for the claim guard failed (%s)", e)
            self._cards = False
        self._ev = self._evidence()

    def _note_items(self) -> list:
        """What the turn's dropped claims come to: a receipt for each that
        holds by now, and one sentence in the server's own words for each
        that still does not, unless text shown after the drop corrected it."""
        if not self._dropped:
            return []
        self._ev = self._evidence()
        receipts: list[dict] = []
        notes: list[str] = []
        seen: set[tuple[str, str]] = set()
        for d in self._dropped:
            key = (d.claim.kind, d.claim.target)
            if key in seen:
                continue
            seen.add(key)
            verdict = check(d.claim, self._ev)
            if verdict.ok:
                if verdict.receipt:
                    receipts.append(verdict.receipt)
            elif not mentions_as_correction(d.claim.target, self._shown[d.shown_at :]):
                notes.append(not_done(d.claim, verdict))
        self._dropped.clear()
        items: list = self._announce(receipts)
        if notes:
            text = "\n\n*" + " ".join(notes) + "*"
            self._shown += text
            items.append(TextDelta(text=text))
        return items


def _cut(text: str, drops: list[str]) -> str:
    for d in drops:
        text = text.replace(d, "", 1)
    return text
