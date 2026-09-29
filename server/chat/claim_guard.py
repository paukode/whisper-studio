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
- a claim does not hold: the sentence is not shown, nor what follows it,
  until the round ends. A round that calls tools waits for them, since a
  sentence may run ahead of the call that makes it true; then (or at once in
  a round with no calls) the sentence is dropped, the rest released, and the
  completion gate sends the model back (deliverables.check_claims). A
  sentence part of which was already shown ends in "...".

When the turn ends (at the end of its last round where no gate can send the
model back: subagents, scheduled and headless runs), each dropped claim that
still does not hold is stated in the server's own words ("Not saved:
`~/a.docx` does not exist."), unless a later sentence named its target (a
correction), so a false claim is never shown as fact. A turn that stops for
an approval hands its held sentences and its start time to the continuation,
which checks them against the calls that ran meanwhile.

The guard keeps every call of the turn it has seen, so compaction or a
salvage round cannot make a done act look undone, and the completion gate
reads the same calls. Every consumer of the runner's frames (the chat,
subagents, scheduled and headless runs, voice) gets the guarded text, and so
does the model's next turn, which is rebuilt from what the chat showed. The
memory agents' notes are not replies and are not guarded.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field, replace

from server.chat.engine.events import (
    Frame,
    Incomplete,
    RoundError,
    RoundResult,
    TextDelta,
    ThinkingStart,
    ToolCall,
    ToolCallProgress,
    ToolCallStart,
)
from server.goals import claims as c
from server.goals.evidence import Call, Evidence, Verdict, check, not_done, word_in
from server.utils import ndjson_dumps

log = logging.getLogger("whisper-studio")

# Text with no sign of a claim trails the stream by this many characters, so
# a sentence whose claim appears within its first words is held whole.
_LOOKBACK = 120
# A block is read back at most this far for the lines that introduce it (a
# list's lead line, a table's header); further back, the lead line is kept.
_BLOCK_BACK = 4000
# Where a sentence ends: a line break, or end punctuation (and a closing
# quote or bracket) before whitespace. After an abbreviation it ends only
# where a capital letter starts the next one ("the docs, etc. The tests...").
_UNIT_BREAK_RE = re.compile(r"\n|[.!?]+[)\]\"'*_`]*(?=\s)")
_ABBREV_END_RE = re.compile(
    r"\b(?:(?P<never>e\.g|i\.e)|etc|vs|cf|approx|mr|mrs|ms|dr|no|inc|ltd|st|fig|eq)\.$",
    re.IGNORECASE,
)
_NEXT_WORD_RE = re.compile(r"\s+(\S)")
_FENCE_RE = re.compile(r"^\s*```", re.MULTILINE)
_LEAD_RE = re.compile(r"^(?!\s*(?:[-*\N{BULLET}]|\d+[.)])\s).*:\s*$")
_TABLE_RULE_RE = re.compile(r"\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*")
# A tool call or thinking begins: the text before it is complete.
_BOUNDARIES = (ToolCallStart, ToolCall, ToolCallProgress, ThinkingStart)
# A continuation of a paused turn keeps the turn's start and its held
# sentences: scope -> (start, the turn's prompt, held sentences).
_PAUSED_STARTS: dict[str, tuple] = {}


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
    by then (a later mention of its target corrects it)."""

    claim: c.Claim
    verdict: Verdict
    shown_at: int


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


class ClaimGuard:
    """One turn's guard (see the module notes). The runner passes each
    round's provider events through ``wrap``, calls ``after_tools`` once the
    round's calls have run, reads ``ledger`` for the gate, and sends what
    ``finish`` returns before the turn's [DONE]."""

    def __init__(
        self,
        *,
        workspace: str | None = None,
        plan_mode: bool = False,
        session_id: str = "",
        enabled: bool = True,
        started_at: float | None = None,
        notes_at_round_end: bool = False,
        carried: list[_Held] | None = None,
    ):
        self.workspace = workspace
        self.plan_mode = plan_mode
        self.session_id = session_id
        self.enabled = enabled
        self.started_at = time.time() if started_at is None else started_at
        # No gate can send the model back (subagents, scheduled and headless
        # runs): the notes close the last round, where the run's report is.
        self.notes_at_round_end = notes_at_round_end
        self._messages: list = []
        self._calls: dict[str, Call] = {}
        self._text_cache: dict[str, str] = {}
        self._ev = Evidence()
        # Whether the session holds an artifact card from an earlier turn:
        # looked up once, off the event loop, the first time a card claim
        # does not hold.
        self._cards: bool | None = None
        self._wants_cards = False
        self._shown = ""
        self._dropped: list[_Dropped] = []
        self._announced: set[tuple[str, str]] = set()
        self._carried = list(carried or [])
        self._carry = False
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
        paused turn keeps its start and its held sentences."""
        from server.goals.deliverables import last_user_prompt

        scope = ctx.turn_scope_id or ctx.session_id
        prompt = last_user_prompt(ctx.messages)
        paused = _PAUSED_STARTS.pop(scope, None)
        resumed = paused is not None and paused[1] == prompt
        policy = getattr(ctx, "policy", None)
        return cls(
            workspace=ctx.ws_path or None,
            plan_mode=ctx.plan_mode,
            session_id=scope,
            enabled=ctx.cost_source != "memory" and _checking_claims(),
            started_at=paused[0] if resumed else None,
            notes_at_round_end=policy is not None and not policy.completion_gate,
            carried=paused[2] if resumed else None,
        )

    def _new_round(self) -> None:
        self._buf = ""
        self._out = 0
        self._judged = 0
        self._fence = False
        self._para = 0
        # The paragraph's list lead line and table header, as (offset, text).
        self._lead = (-1, "")
        self._table = (-1, "")
        self._last_line = (-1, "")
        self._fenced_lines: list[tuple[int, bool]] = [(0, False)]
        self._held: list[_Held] = []
        self._drops: list[tuple[int, int, str]] = []
        self._seen: set[tuple[int, str, str]] = set()
        self._receipts: list[tuple[int, dict]] = []
        self._scanned = 0
        self._hot = False

    def _evidence(self) -> Evidence:
        ev = Evidence.of(
            self._messages,
            workspace=self.workspace,
            started_at=self.started_at,
            session_has_artifact=bool(self._cards),
            plan_mode=self.plan_mode,
            extra_calls=list(self._calls.values()),
            cache=self._text_cache,
        )
        for call in ev.calls:
            self._calls[call.id] = call
        return ev

    def ledger(self) -> list[Call]:
        """Every call of the turn the guard has seen, for the gate."""
        return list(self._calls.values())

    # ── the runner's three calls ─────────────────────────────────────────

    async def wrap(self, events, messages: list):
        """One round's provider events, with its text held as the module
        notes say. A round that fails shows what it said so far before its
        error, as it did unguarded: that text counts as streamed, so the
        round is not run again over it."""
        if not self.enabled:
            async for ev in events:
                yield ev
            return
        for item in self._begin_round(messages):
            yield item
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
                if isinstance(ev, _BOUNDARIES):
                    for item in self._close_tail() + self._release(len(self._buf)):
                        yield item
                elif isinstance(ev, (RoundResult, Incomplete, RoundError)):
                    cut = isinstance(ev, RoundResult) and ev.stop_reason in (
                        "max_tokens",
                        "pause_turn",
                    )
                    tools = isinstance(ev, RoundResult) and _calls_tools(ev.content) and not cut
                    self._holding = ev if isinstance(ev, RoundResult) else None
                    if self._wants_cards and self._cards is None:
                        await self._load_cards()
                    last = isinstance(ev, RoundResult) and not tools and not cut
                    for item in self._end_round(final=not tools, last=last):
                        yield item
                    # A cut round goes on in the next one: its text is kept
                    # there for context, all of it already settled.
                    self._carry = cut
                yield ev
                self._holding = None
        except Exception:
            for item in self._end_round(final=True, last=False):
                yield item
            raise

    def held_usage(self):
        """The usage of a round that finished while its held text was still
        being sent (the adapter already handed its cost over), else None."""
        return self._holding.usage if self._holding is not None else None

    def after_tools(self, messages: list) -> list[str]:
        """The round's calls have run: check its held sentences again, drop
        those that still do not hold and release the rest."""
        if not self.enabled:
            return []
        self._messages = messages
        return _sse(self._settle(final=True))

    def finish(self, *, paused: bool = False) -> list[str]:
        """The frames to send before the turn's [DONE]: the text still held,
        and a note for each dropped claim that still does not hold. A turn
        that paused for an approval hands its held sentences on instead."""
        if not self.enabled:
            return []
        items = self._close_tail()
        if paused:
            from server.goals.deliverables import last_user_prompt

            items += self._settle(final=True, stash=True)
            _PAUSED_STARTS[self.session_id] = (
                self.started_at,
                last_user_prompt(self._messages),
                self._carried,
            )
            self._carried = []
            self._dropped.clear()
            return _sse(items)
        items += self._settle(final=True)
        return _sse(items + self._note_items())

    # ── reading the stream ───────────────────────────────────────────────

    def _begin_round(self, messages: list) -> list:
        items: list = []
        if self._carry:
            self._carry = False
            self._out = self._judged = len(self._buf)
        elif self._round_done:
            # A finished round whose calls never reported back.
            items = self._settle(final=True)
            self._new_round()
        else:
            # An attempt that ended without a result is regenerated.
            self._new_round()
        self._round_done = False
        self._holding = None
        self._messages = messages
        self._ev = self._evidence()
        return items + self._resume_carried()

    def _resume_carried(self) -> list:
        """The sentences a paused turn held: shown now if a call that ran
        meanwhile made them true, else noted at the end like any other."""
        if not self._carried:
            return []
        items: list = []
        for h in self._carried:
            failures = [(cl, v) for cl, _was in h.failures if not (v := check(cl, self._ev)).ok]
            receipts = list(h.receipts) + [
                v.receipt for cl, _was in h.failures if (v := check(cl, self._ev)).ok and v.receipt
            ]
            if failures:
                self._dropped += [_Dropped(cl, v, len(self._shown)) for cl, v in failures]
            else:
                self._shown += h.text
                items.append(TextDelta(text=h.text))
            items += self._announce(receipts)
        self._carried = []
        return items

    def _feed(self, text: str) -> list:
        self._buf += text
        items: list = []
        while (end := self._unit_end(self._judged)) is not None:
            items += self._close_unit(self._judged, end)
            self._judged = end
        return items + self._release_tail()

    def _unit_end(self, start: int) -> int | None:
        at = start
        while m := _UNIT_BREAK_RE.search(self._buf, at):
            if m.group(0) == "\n":
                return m.end()
            abbrev = _ABBREV_END_RE.search(self._buf[max(0, m.start() - 7) : m.end()])
            if not abbrev:
                return m.end()
            if not abbrev.group("never"):
                nxt = _NEXT_WORD_RE.match(self._buf, m.end())
                if nxt is None:
                    return None
                if nxt.group(1).isupper():
                    return m.end()
            at = m.end()
        return None

    def _block_start(self, at: int) -> tuple[int, str]:
        """Where the block holding offset ``at`` starts outside a code block
        (the paragraph, at most _BLOCK_BACK back), and the lead line to read
        ahead of it when the window leaves it out."""
        start = self._para
        if at - start > _BLOCK_BACK:
            start = next(
                (s for s, fenced in self._fenced_lines if s >= at - _BLOCK_BACK and not fenced), at
            )
        kept = [text for at_, text in (self._lead, self._table) if 0 <= at_ < start]
        return start, "\n".join(kept)

    def _close_unit(self, start: int, end: int) -> list:
        in_code = self._fence
        if len(_FENCE_RE.findall(self._buf[start:end])) % 2:
            self._fence = not self._fence
        self._note_lines(start, end)
        self._scanned, self._hot = end, False
        if in_code or self._fence:
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

    def _note_lines(self, start: int, end: int) -> None:
        """Keep where lines start (and whether inside a code block), where
        the paragraph starts and its lead line, for ``_block_start``."""
        at = self._buf.rfind("\n", 0, start) + 1
        while True:
            nl = self._buf.find("\n", at, end)
            if nl < 0:
                break
            line = self._buf[at:nl]
            nxt = nl + 1
            self._fenced_lines.append((nxt, self._fence))
            if not self._fence and not line.strip():
                self._para, self._lead, self._table = nxt, (-1, ""), (-1, "")
            elif not self._fence and _LEAD_RE.match(line):
                self._lead = (at, line)
            elif not self._fence and "|" in line and _TABLE_RULE_RE.fullmatch(line):
                head_at, head = self._last_line
                if "|" in head:
                    self._table = (head_at, f"{head}\n{line}")
            self._last_line = (at, line)
            at = nxt
        if len(self._fenced_lines) > 4000:
            self._fenced_lines = self._fenced_lines[-2000:]

    def _close_tail(self) -> list:
        """The sentence in progress, read as ended (the round ended or the
        model turned to a tool)."""
        if self._judged >= len(self._buf):
            return []
        items = self._close_unit(self._judged, len(self._buf))
        self._judged = len(self._buf)
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
        if self._fence:
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
        the dropped sentences, then the receipts of what was sent."""
        stop = min([h.start for h in self._held] + [limit])
        items: list = []
        if stop > self._out:
            text = self._visible(self._out, stop)
            self._out = stop
            if text:
                self._shown += text
                items.append(TextDelta(text=text))
        due = [r for at, r in self._receipts if at <= self._out]
        self._receipts = [(at, r) for at, r in self._receipts if at > self._out]
        return items + self._announce(due)

    def _visible(self, start: int, end: int) -> str:
        parts: list[str] = []
        at = start
        for s, e, instead in sorted(self._drops):
            if e <= at or s >= end:
                continue
            if s > at:
                parts.append(self._buf[at:s])
            parts.append(instead)
            at = max(at, e)
        if at < end:
            parts.append(self._buf[at:end])
        return "".join(parts)

    def _announce(self, receipts: list[dict]) -> list:
        fresh = []
        for r in receipts:
            key = (r.get("kind", ""), r.get("target", ""))
            if key not in self._announced:
                self._announced.add(key)
                fresh.append(r)
        return [Frame({"deliveries": {"items": fresh}})] if fresh else []

    # ── settling what was held ───────────────────────────────────────────

    def _end_round(self, *, final: bool, last: bool) -> list:
        items = self._close_tail()
        self._round_done = True
        if not final:
            return items + self._release(len(self._buf))
        items += self._settle(final=True)
        if last and self.notes_at_round_end:
            # Where no gate can send the model back, the notes belong to the
            # run's last round: its report.
            items += self._note_items()
        return items

    def _settle(self, *, final: bool, stash: bool = False) -> list:
        """Check the held sentences again (a call may have made one true):
        release those that hold, and with ``final`` drop the others (or, with
        ``stash``, hand them to a paused turn's continuation) and release
        everything after them."""
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
            if not final:
                still.append(replace(h, failures=failures))
                continue
            # The space ahead of the sentence stays; the space after it goes
            # with it, and a line of its own takes its line break too.
            start, end = h.start, h.end
            while start < end and self._buf[start] in " \t":
                start += 1
            while end < len(self._buf) and self._buf[end] in " \t":
                end += 1
            if start == 0 or self._buf[start - 1] == "\n":
                if end < len(self._buf) and self._buf[end] == "\n" and not h.text.endswith("\n"):
                    end += 1
            self._drops.append((start, end, "... " if h.partial else ""))
            self._receipts += [(h.end, r) for r in h.receipts]
            if stash:
                self._carried.append(replace(h, failures=failures))
                continue
            self._dropped += [_Dropped(cl, v, len(self._shown)) for cl, v in failures]
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

    def _corrected(self, d: _Dropped) -> bool:
        """True when text shown after the drop names the claim's target, as
        a whole word ("the remaining docs" names no branch `main`)."""
        target = d.claim.target.strip().strip("`")
        if not target:
            return False
        later = self._shown[d.shown_at :]
        name = target.rstrip("/").rsplit("/", 1)[-1]
        return word_in(target, later) or (len(name) >= 4 and word_in(name, later))

    def _note_items(self) -> list:
        """What the turn's dropped claims come to: a receipt for each that
        holds by now, and one sentence in the server's own words for each
        that still does not."""
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
            elif not self._corrected(d):
                notes.append(not_done(d.claim, verdict))
        self._dropped.clear()
        items: list = self._announce(receipts)
        if notes:
            text = "\n\n*" + " ".join(notes) + "*"
            self._shown += text
            items.append(TextDelta(text=text))
        return items
