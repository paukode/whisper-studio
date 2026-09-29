"""Hold back a sentence that claims a delivery until the claim is verified.

The model streams its reply as it writes it, so a sentence saying a file was
saved, a branch pushed or a message sent reached the user before anything
checked it, and in real sessions such a sentence was false. The guard sits
between the provider's text and the SSE text frames (runner.py). Text flows
through at once, except that the sentence in progress is held while it could
still become a claim (it names a path or an address, or has a verb of
delivering), and any other text trails the stream by a few words, so a claim
that shows up early in its sentence is held from the sentence's start. When a
sentence ends, server/goals/claims.py reads it and server/goals/evidence.py
checks each claim it makes:

- no claim, or every claim holds: the sentence is released, and a
  ``deliveries`` frame lists what was verified (the chat shows chips);
- a claim does not hold: the sentence is not shown, nor what follows it,
  until the round ends. A round that calls tools waits for them, since a
  sentence may run ahead of the call that makes it true; then (or at once in
  a round with no calls) the sentence is dropped and the rest released, and
  the completion gate sends the model back to do the work or correct itself
  (deliverables.check_claims).

When the turn ends, each dropped claim that still does not hold is stated in
the server's own words ("Not saved: `~/a.docx` does not exist."), unless a
later sentence named its target (a correction), so a false claim is never
shown as fact. A turn that stops for an approval notes nothing: its
continuation, which keeps the turn's start time, makes its own claims.

Every consumer of the runner's frames (the chat, subagents, scheduled and
headless runs, voice) gets the guarded text, and so does the model's next
turn, which is rebuilt from what the chat showed. The memory agents' notes
are not replies and are not guarded.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, replace

from server.chat.engine.events import Frame, Incomplete, RoundError, RoundResult, TextDelta
from server.goals import claims as c
from server.goals.evidence import Evidence, Verdict, check, not_done
from server.utils import ndjson_dumps

log = logging.getLogger("whisper-studio")

# Text with no sign of a claim trails the stream by this many characters, so
# a sentence whose claim appears within its first words is held whole.
_LOOKBACK = 80
# A block is read back at most this far for the lines that introduce it (a
# list's lead line, a table's header).
_BLOCK_BACK = 4000
# What may make the sentence in progress a claim: a path, an address, a link,
# code, a number sign, or a verb of making, removing or delivering.
_TRIGGER_RE = re.compile(
    r"[~/`\[#]|://|\b\w+/\w"
    r"|\b(?:saved|wrote|written|created|exported|generated|produced|stored|placed|put|rendered"
    r"|built|converted|updated|downloaded|attached|copied|moved|renamed|edited|modified|changed"
    r"|fixed|added|refactored|patched|replaced|appended|inserted|restored|drafted|compiled"
    r"|deleted|removed|trashed|erased|pushed|committed|merged|opened|raised|submitted|filed"
    r"|logged|uploaded|synced|sent|emailed|e-mailed|messaged|posted|replied|forwarded|pinged"
    r"|texted|notified|deployed|redeployed|published|released|scheduled|set|switched|enabled"
    r"|disabled|toggled|turned|noted|recorded|kept|artifact|here|ready|available|find|located"
    r"|lives|live|PRs?|pull request|memory)\b",
    re.IGNORECASE,
)
# Where a sentence ends: a line break, or end punctuation (and a closing
# quote or bracket) before whitespace, but not after an abbreviation.
_UNIT_BREAK_RE = re.compile(r"\n|[.!?]+[)\]\"'*_`]*(?=\s)")
_ABBREV_END_RE = re.compile(
    r"\b(?:e\.g|i\.e|etc|vs|cf|approx|mr|mrs|ms|dr|no|inc|ltd|st|fig|eq)\.$", re.IGNORECASE
)
_FENCE_RE = re.compile(r"^\s*```", re.MULTILINE)
# A continuation of a paused turn keeps the turn's start: session -> (start,
# the turn's prompt).
_PAUSED_STARTS: dict[str, tuple[float, str]] = {}


@dataclass
class _Held:
    """A sentence whose claims did not hold when it ended."""

    start: int
    end: int
    failures: list[tuple[c.Claim, Verdict]]


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
    round's calls have run, and sends what ``finish`` returns before the
    turn's [DONE]."""

    def __init__(
        self,
        *,
        workspace: str | None = None,
        plan_mode: bool = False,
        session_id: str = "",
        enabled: bool = True,
        started_at: float | None = None,
    ):
        self.workspace = workspace
        self.plan_mode = plan_mode
        self.session_id = session_id
        self.enabled = enabled
        self.started_at = time.time() if started_at is None else started_at
        self._messages: list = []
        self._ev = Evidence()
        # Whether the session holds an artifact card from an earlier turn:
        # looked up once, off the event loop, the first time a card claim
        # does not hold.
        self._cards: bool | None = None
        self._wants_cards = False
        self._shown = ""
        self._dropped: list[_Dropped] = []
        self._announced: set[tuple[str, str]] = set()
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
        paused turn keeps its start."""
        from server.goals.deliverables import last_user_prompt

        scope = ctx.turn_scope_id or ctx.session_id
        prompt = last_user_prompt(ctx.messages)
        paused = _PAUSED_STARTS.pop(scope, None)
        return cls(
            workspace=ctx.ws_path or None,
            plan_mode=ctx.plan_mode,
            session_id=scope,
            enabled=ctx.cost_source != "memory" and _checking_claims(),
            started_at=paused[0] if paused and paused[1] == prompt else None,
        )

    def _new_round(self) -> None:
        self._buf = ""
        self._out = 0
        self._judged = 0
        self._fence = False
        self._held: list[_Held] = []
        self._drops: list[tuple[int, int]] = []
        self._seen: set[tuple[int, str, str]] = set()
        self._receipts: list[tuple[int, dict]] = []

    def _evidence(self) -> Evidence:
        return Evidence.of(
            self._messages,
            workspace=self.workspace,
            started_at=self.started_at,
            session_has_artifact=bool(self._cards),
            plan_mode=self.plan_mode,
        )

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
                if isinstance(ev, RoundResult) and ev.stop_reason == "max_tokens":
                    # The next round continues this text where it was cut.
                    self._carry = True
                elif isinstance(ev, (RoundResult, Incomplete, RoundError)):
                    tools = isinstance(ev, RoundResult) and _calls_tools(ev.content)
                    self._holding = ev if isinstance(ev, RoundResult) else None
                    if self._wants_cards and self._cards is None:
                        await self._load_cards()
                    for item in self._end_round(final=not tools):
                        yield item
                yield ev
                self._holding = None
        except Exception:
            for item in self._end_round(final=True):
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
        and a note for each dropped claim that still does not hold."""
        if not self.enabled:
            return []
        items = self._close_tail() + self._settle(final=True)
        if paused:
            from server.goals.deliverables import last_user_prompt

            _PAUSED_STARTS[self.session_id] = (self.started_at, last_user_prompt(self._messages))
            self._dropped.clear()
            return _sse(items)
        return _sse(items) + self._notes()

    # ── reading the stream ───────────────────────────────────────────────

    def _begin_round(self, messages: list) -> list:
        items: list = []
        if self._carry:
            self._carry = False
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
            if m.group(0) == "\n" or not _ABBREV_END_RE.search(
                self._buf[max(0, m.start() - 7) : m.end()]
            ):
                return m.end()
            at = m.end()
        return None

    def _block_start(self, at: int) -> int:
        start = self._buf.rfind("\n\n", 0, at)
        start = 0 if start < 0 else start + 2
        if at - start > _BLOCK_BACK:
            start = self._buf.find("\n", at - _BLOCK_BACK, at) + 1 or at - _BLOCK_BACK
        return start

    def _close_unit(self, start: int, end: int) -> list:
        in_code = self._fence
        if len(_FENCE_RE.findall(self._buf[start:end])) % 2:
            self._fence = not self._fence
        if in_code or self._fence:
            return self._release(end)
        receipts, failures = self._judge(start, end)
        if failures:
            self._held.append(_Held(max(start, self._out), end, failures))
            return self._release(end)
        self._receipts += [(end, r) for r in receipts]
        return self._release(end)

    def _close_tail(self) -> list:
        """The round's last sentence, which no break ended, read as ended."""
        if self._judged >= len(self._buf):
            return []
        items = self._close_unit(self._judged, len(self._buf))
        self._judged = len(self._buf)
        return items

    def _judge(self, start: int, end: int) -> tuple[list[dict], list[tuple[c.Claim, Verdict]]]:
        """Check the claims of the sentence [start, end), reading its block
        from the start so a list item keeps its lead line."""
        block = self._block_start(start)
        receipts: list[dict] = []
        failures: list[tuple[c.Claim, Verdict]] = []
        for claim in c.read_claims(self._buf[block:end]):
            s, e = block + claim.start, block + claim.end
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
        if _TRIGGER_RE.search(self._buf, self._judged):
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
        for s, e in sorted(self._drops):
            if e <= at or s >= end:
                continue
            if s > at:
                parts.append(self._buf[at:s])
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

    def _end_round(self, *, final: bool) -> list:
        items = self._close_tail()
        self._round_done = True
        if final:
            return items + self._settle(final=True)
        return items

    def _settle(self, *, final: bool) -> list:
        """Check the held sentences again (a call may have made one true):
        release those that hold, and with ``final`` drop the others and
        release everything after them."""
        if self._held:
            self._ev = self._evidence()
        still: list[_Held] = []
        for h in self._held:
            failures: list[tuple[c.Claim, Verdict]] = []
            for claim, _was in h.failures:
                verdict = check(claim, self._ev)
                if verdict.ok:
                    if verdict.receipt:
                        self._receipts.append((h.end, verdict.receipt))
                else:
                    failures.append((claim, verdict))
            if not failures:
                continue
            if not final:
                still.append(_Held(h.start, h.end, failures))
                continue
            end = h.end
            while end < len(self._buf) and self._buf[end] in " \t":
                end += 1
            self._drops.append((h.start, end))
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
        """True when text shown after the drop names the claim's target."""
        target = d.claim.target.strip()
        if not target:
            return False
        later = self._shown[d.shown_at :]
        return target in later or os.path.basename(target.rstrip("/")) in later

    def _notes(self) -> list[str]:
        """What the turn's dropped claims come to at its end: a receipt for
        each that holds by now, and one sentence in the server's own words
        for each that still does not."""
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
        return _sse(items)
