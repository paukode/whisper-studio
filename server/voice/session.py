"""One live voice conversation with Nova Sonic.

Owns the duplex stream, the running transcript, tool dispatch and the
eight-minute stream renewal. Talks to the transport layer (routes.py) only
through ``emit(event)`` callbacks, so it is testable with a fake stream.

Events emitted (dicts; ``audio`` carries raw PCM16 bytes, the rest are JSON-safe):

    ready            stream open; carries model_id and voice_id
    state            "listening" | "speaking" | "thinking"
    user_transcript  final text of what the user said
    assistant_text   text=..., final=False while being spoken (speculative,
                     incremental), final=True once for the whole utterance
    audio            pcm=bytes of 24 kHz mono PCM16 assistant speech
    interrupted      user barged in; the browser must flush its playback queue
    tool_call        Sonic asked for a tool (tool_use_id, name, input)
    assistant_step   progress inside ask_assistant (name, status, detail)
    tool_result      the tool's answer (tool_use_id, name, output, status)
    client_action    an app control for the browser (action, value)
    usage            cumulative token counts
    renewing/renewed the 8-minute stream was swapped for a fresh one
    error            message
    ended            reason
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from array import array
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from server.voice import protocol
from server.voice.sonic_client import SonicUnavailable, open_stream
from server.voice.tools import VOICE_TOOLS, WORKING_PREFIX, ToolContext, run_tool

log = logging.getLogger(__name__)

EmitFn = Callable[[dict[str, Any]], Awaitable[None]]
OpenFn = Callable[[str, str], Awaitable[Any]]

# Bedrock closes a bidirectional stream after 8 minutes. Renew comfortably
# before that, at a quiet moment, so the user never notices.
STREAM_HARD_LIMIT_S = 480
RENEW_AFTER_S = 390
RENEW_DEADLINE_S = 460
TRANSCRIPT_LIMIT = 60
MAX_REOPENS = 6
# A stream that lived this long was healthy: the reopen budget starts over.
REOPEN_RESET_S = 60
# Sonic drops a stream after 295 s without input it counts (non-silent audio or
# text); observed live: all-zero frames do not count, so a muted microphone or
# a quiet room would end the conversation. Renew before that, at a quiet moment.
IDLE_RENEW_S = 240
IDLE_DEADLINE_S = 280
# One assistant message per utterance. Observed live: Sonic sends the
# SPECULATIVE text of the whole utterance within about a second, streams all the
# audio within a few seconds, then confirms what it said as FINAL text chunks at
# speaking pace (chunks 4 to 9 s apart, the last landing where the audio ends).
# Committing per chunk made the live text vanish and come back piecewise, so the
# utterance is committed ONCE when its speech has ended: at the estimated end of
# the audio plus a quiet margin, at the hard cap after the last event, or at
# once on a turn boundary (user speech, next utterance, completion boundaries).
UTTERANCE_QUIET_S = 2.0
UTTERANCE_MAX_WAIT_S = 20.0
INTERRUPT_GRACE_S = 2.0
OUT_BYTES_PER_S = 48000  # 24 kHz mono PCM16
# Confirmed text this much shorter than the preview means a chunk is still to
# come; the preview (what the user heard) is committed instead.
CONFIRMED_COMPLETE_RATIO = 0.8
# After a hang-up, a run paused on the user's decision keeps its card (and the
# socket) this long before it is given up.
PENDING_DRAIN_S = 900.0
# A FINAL chunk with no utterance in progress this soon after a commit is a late
# confirmation of that commit (Sonic never confirms without a preview first).
LATE_FINAL_WINDOW_S = 60.0
# The browser gets the full written answer; Sonic only needs enough to speak
# a summary, so its copy is trimmed.
SPOKEN_RESULT_CHARS = 2500


@dataclass
class VoiceConfig:
    model_id: str = protocol.DEFAULT_MODEL_ID
    region: str = "us-east-1"
    voice_id: str = protocol.DEFAULT_VOICE_ID
    endpointing: str = protocol.DEFAULT_ENDPOINTING
    system_prompt: str = ""
    max_tokens: int = 1024


SYSTEM_PROMPT = """You are the voice of Whisper Studio, a desktop app for meetings, transcripts, code and documents. The user is talking to you out loud and hears your words spoken, so keep every reply to one or two short sentences of natural speech: no lists, no markdown, no code, and never read file paths or URLs aloud (say "the parser file" instead).

You are the front desk, not the expert. For anything that needs real work, such as opening or reading folders and files, editing, running commands or tests, git and branches, documents, searching, or any question about the user's workspace, transcript or project, call ask_assistant right away with one clear written request that keeps the user's exact words for names, paths and commands, and captures everything they asked even if they said it in several pieces. Do not ask the user clarifying questions first: the assistant can look things up itself. People spell names out loud: "m l dash o p s" is the folder ml-ops, "git branch" is a git command. Say briefly that you are on it and keep listening. The assistant's full written answer is shown on the user's screen automatically, so when it comes back give the gist in one or two sentences and, if they ask to see the output or details, tell them it is on screen.

Only when an ask_assistant result says the assistant is waiting for the user's approval or answer: tell the user exactly what it wants in one sentence, ask yes or no (or the question), then call resolve_request with approve, deny, or their answer. Never call resolve_request at any other time, and never approve on the user's behalf; "uh huh" or "okay" while you are still working is not an approval. While ask_assistant is running, do not call it again for the same request; if the user asks what is happening, say it is still working. If a result reports an error, say so plainly.

Long tasks keep running in the background: when ask_assistant answers "Working on it", tell the user in one sentence that it is running and may take a while, then keep listening and handle anything else they ask. When a message starting with [Result of the request ...] arrives, summarize it in one or two sentences; when one starting with [The assistant needs the user's decision ...] arrives, ask the user and call resolve_request.

Never say that something was done, deleted, pushed, created or finished unless a tool result told you so in this conversation; "Working on it" means it is still running, not done. If the user asks what happened or what is running, call background_status instead of guessing. Speech recognition confuses similar words (pull and push, delete and delegate, branch and brunch): when the user asks for anything that changes or removes something, say the exact action back in your own sentence ("pulling, not pushing") so they can correct you, and pass their exact words to ask_assistant.

Do not repeat yourself: never say again what you already told the user (that something is running, that a result will arrive, what a result said). When a message arrives and nothing new happened, say so in a few words or stay silent. A late result or a decision request gets one sentence.

Never read lists, file names, branch names, paths, code or command output aloud. The full answer is already on the user's screen: say what there is in one sentence (for example how many branches and which one is current) and that the list is on screen. If the user says stop, wait or that's enough, stop talking immediately and listen.

Use control_recording when the user asks to start or stop recording a meeting. Call end_conversation when the user says stop, goodbye, that they are done, or asks to type instead. For greetings and small talk just answer. If you did not catch something, ask the user to say it again.{context}"""


def build_system_prompt(*, workspace_name: str | None = None, now: datetime | None = None) -> str:
    bits = [f" Today is {(now or datetime.now()).strftime('%A, %B %d, %Y')}."]
    if workspace_name:
        bits.append(f" The connected workspace is {workspace_name}.")
    return SYSTEM_PROMPT.format(context="".join(bits))


@dataclass
class _Live:
    """The currently open stream and the ids the model knows it by."""

    stream: Any
    prompt_name: str
    audio_name: str
    opened_at: float
    # Last time Sonic received input it counts (see IDLE_RENEW_S).
    last_input_at: float = 0.0
    pump: asyncio.Task | None = None
    closing: bool = False
    tool_ids: set[str] = field(default_factory=set)
    # Model events received on this stream. Zero when it dies means Bedrock
    # never answered: the failure is reported as a start failure, not retried.
    events: int = 0


@dataclass
class _Utterance:
    """One thing Sonic says: its preview, its confirmed chunks, and the timing
    needed to know when the speech has ended."""

    id: str
    started_at: float
    last_event_at: float
    spec_cids: set[str] = field(default_factory=set)
    spec_parts: list[str] = field(default_factory=list)
    final_parts: list[str] = field(default_factory=list)
    first_audio_at: float | None = None
    audio_bytes: int = 0
    interrupted_at: float | None = None
    timer: asyncio.Task | None = None

    def speech_end(self, now: float) -> float:
        if self.first_audio_at is None:
            return now
        return self.first_audio_at + self.audio_bytes / OUT_BYTES_PER_S


class VoiceSession:
    def __init__(
        self,
        *,
        session_id: str,
        config: VoiceConfig,
        emit: EmitFn,
        history: list[dict[str, Any]] | None = None,
        model_key: str | None = None,
        session_approvals: dict[str, Any] | None = None,
        opener: OpenFn | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session_id = session_id
        self.config = config
        self.emit = emit
        self.model_key = model_key
        self.session_approvals: dict[str, Any] = dict(session_approvals or {})
        # The delegated assistant's unanswered request (approval, question,
        # folder). Shared with every ToolContext so resolve_request sees it.
        self.pending: dict[str, Any] = {}
        # Delegated runs in flight and pauses queued behind the current one
        # (see ToolContext.runs / pending_queue).
        self.runs: dict[str, dict[str, Any]] = {}
        self.pending_queue: list[dict[str, Any]] = []
        self._open_stream = opener
        self._clock = clock
        self.transcript: list[dict[str, str]] = [
            {"role": r["role"], "content": str(r["content"])}
            for r in (history or [])
            if r.get("role") in ("user", "assistant") and str(r.get("content") or "").strip()
        ][-TRANSCRIPT_LIMIT:]
        self.done = asyncio.Event()
        self.end_reason: str | None = None
        self._live: _Live | None = None
        self._send_lock = asyncio.Lock()
        self._renew_task: asyncio.Task | None = None
        # Set once any stream has delivered a model event. Until then a dying
        # stream is a configuration or access problem (wrong region, model not
        # enabled, blocked transport) and reopening it six times only delays
        # the real message; see _stream_failed.
        self._ever_healthy = False
        self._last_stream_error = ""
        # Sonic-side work (its tool calls, UI resolves, the goodbye timer):
        # cancelled when the conversation ends.
        self._tool_tasks: set[asyncio.Task] = set()
        # Delegated runs and their late deliveries: they outlive a hang-up and
        # are only cancelled when the browser is gone (see stop).
        self._run_tasks: set[asyncio.Task] = set()
        self._pending_tools = 0
        self._speaking = False
        self._state = "listening"
        self._stopping = False
        self._reopens = 0
        self._spec_buf: dict[str, list[str]] = {}
        self._final_buf: dict[str, list[str]] = {}
        # The utterance in progress (see _Utterance) and what was last committed.
        self._utt: _Utterance | None = None
        self._last_committed = ""
        self._last_commit_at = float("-inf")

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        await self._open(first=True)
        await self.emit(
            {"type": "ready", "model_id": self.config.model_id, "voice_id": self.config.voice_id}
        )
        # Announce the initial state unconditionally so the browser can render
        # the voice bar from the first event pair.
        self._state = "listening"
        await self.emit({"type": "state", "state": "listening"})
        self._renew_task = asyncio.create_task(self._renew_loop())

    async def stop(self, reason: str = "user", *, cancel_runs: bool = False) -> None:
        """Hang up: close the Sonic stream and Sonic's own work. Delegated runs
        still in flight are NOT discarded: the browser is told (``draining``)
        and their steps, requests and answers keep flowing over the socket until
        they finish, then ``ended`` follows. ``cancel_runs`` is for a browser
        that is gone (socket closed): nothing could receive the answers."""
        if self._stopping:
            if cancel_runs:
                self._cancel_runs()
            return
        self._stopping = True
        self.end_reason = reason
        await self._end_utterance("stop")
        # stop() may run inside the renew loop (a failed renewal) or a tool
        # task (end_conversation): never cancel the task we are running in, or
        # the rest of this method (ended, done) would be skipped.
        if self._renew_task and self._renew_task is not asyncio.current_task():
            self._renew_task.cancel()
        for task in list(self._tool_tasks):
            if task is not asyncio.current_task():
                task.cancel()
        live, self._live = self._live, None
        if live is not None:
            await self._close_live(live, graceful=True)
        if cancel_runs:
            # The browser is gone: nothing can receive the answers. Cancel and
            # let the cancellations land before reporting the end.
            self._cancel_runs()
            if self._run_tasks:
                await asyncio.wait(set(self._run_tasks), timeout=5)
        elif self._run_tasks or self.pending or self.pending_queue:
            # Work in flight, or a run waiting for the user's answer on the
            # card: keep the socket until it is done (or answered and done).
            runs = [
                {"run_id": rid, "request": str(info.get("request", ""))}
                for rid, info in self.runs.items()
            ]
            await self.emit({"type": "draining", "runs": runs})
            waited = 0.0
            while True:
                if self._run_tasks:
                    await asyncio.wait(set(self._run_tasks))
                    continue
                if (self.pending or self.pending_queue) and waited < PENDING_DRAIN_S:
                    await asyncio.sleep(0.25)
                    waited += 0.25
                    continue
                break
        self._forget_pauses()
        await self.emit({"type": "ended", "reason": reason})
        self.done.set()

    def _cancel_runs(self) -> None:
        self.pending.clear()
        self.pending_queue.clear()
        for task in list(self._run_tasks):
            if task is not asyncio.current_task():
                task.cancel()

    def _forget_pauses(self) -> None:
        """Paused delegated turns nobody can resume once voice mode ends: drop
        their stashed state instead of keeping it for the process lifetime."""
        from server.chat.engine.pause import paused_sessions

        for req in [self.pending, *self.pending_queue]:
            run_id = req.get("run_id") if req else None
            if run_id:
                paused_sessions.pop(f"exec:{run_id}", None)
        self.pending.clear()
        self.pending_queue.clear()

    def request_end(self) -> None:
        """Called by the end_conversation tool. Give Sonic a moment to speak
        its goodbye, then stop."""

        async def _later() -> None:
            await asyncio.sleep(2.5)
            await self.stop("assistant")

        self._register_task(asyncio.create_task(_later()))

    def _tool_context(self) -> ToolContext:
        return ToolContext(
            session_id=self.session_id,
            model_key=self.model_key,
            emit=self.emit,
            request_end=self.request_end,
            pending=self.pending,
            session_approvals=self.session_approvals,
            runs=self.runs,
            pending_queue=self.pending_queue,
            deliver_late=self._deliver_late,
            register_task=self._register_task,
        )

    def _register_task(self, task: asyncio.Task) -> None:
        self._run_tasks.add(task)
        task.add_done_callback(self._run_tasks.discard)

    async def _deliver_late(self, text: str) -> None:
        """A background run finished (or paused): tell Sonic as a typed turn so
        it can speak the outcome, exactly like a renewal reroute. Typed text
        interrupts Sonic mid-sentence, so wait until it has finished talking."""
        await self._await_quiet()
        live = await self._await_live()
        if live is None:
            log.warning("voice: no live stream to deliver a background result to")
            return
        for ev in protocol.text_content(live.prompt_name, "USER", text, interactive=True):
            await self._send(live, ev)

    async def _await_quiet(self, timeout_s: float = 45.0) -> None:
        """Wait for the utterance in progress (if any) to end."""
        deadline = self._clock() + timeout_s
        loops = 0
        while self._utt is not None and not self._stopping:
            loops += 1
            if self._clock() >= deadline or loops > int(timeout_s / 0.1) + 1:
                return
            await asyncio.sleep(0.1)

    async def _await_live(self, timeout_s: float = 20.0) -> _Live | None:
        """The live stream, waiting out a renewal in progress (the old stream is
        closing, the new one not open yet) so nothing typed to Sonic is lost."""
        deadline = self._clock() + timeout_s
        loops = 0
        while not self._stopping:
            live = self._live
            if live is not None and not live.closing:
                return live
            loops += 1
            if self._clock() >= deadline or loops > int(timeout_s / 0.05) + 1:
                return None
            await asyncio.sleep(0.05)
        return None

    def _result_status(self, output: str, name: str = "ask_assistant") -> str:
        if output.startswith("Error:"):
            return "error"
        # The run outlived ask_assistant's wait and continues in the background;
        # its answer arrives later as an assistant_answer event.
        if output.startswith(WORKING_PREFIX):
            return "working"
        # A delegated run that stopped for the user's decision is not an answer
        # yet; the browser shows the request card instead of a bubble. Only the
        # delegating tools can pause; an unrelated tool's result stays "ok".
        if name in ("ask_assistant", "resolve_request") and self.pending:
            return "paused"
        return "ok"

    def resolve_pending_from_ui(self, decision: str) -> None:
        """The user clicked Yes/No (or an option) on the request card instead of
        saying it. Resolve exactly like the tool would, then tell Sonic what
        happened as a typed turn so it can speak the outcome."""
        # Allowed while draining too: a background run may pause after the
        # user hung up, and the card on screen is then the only way to answer.
        if not self.pending or self.done.is_set():
            return
        task = asyncio.create_task(self._resolve_from_ui(decision))
        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_tasks.discard)

    async def _resolve_from_ui(self, decision: str) -> None:
        self._pending_tools += 1
        await self._refresh_state()
        # Announce the call like Sonic's own so the browser's pending count and
        # activity trace stay balanced.
        tool_use_id = f"ui-{protocol.new_id()[:8]}"
        await self.emit(
            {
                "type": "tool_call",
                "tool_use_id": tool_use_id,
                "name": "resolve_request",
                "input": {"decision": decision},
            }
        )
        # Tell Sonic right away, or it keeps waiting for a spoken yes/no while
        # the assistant continues (observed live: "Please say yes or no.").
        live = self._live
        if live is not None and not self._stopping:
            heads_up = (
                f"[The user already answered the pending request on screen: {decision}. It is "
                "being processed; do NOT call resolve_request. Acknowledge in a few words and "
                "wait for the outcome.]"
            )
            for ev in protocol.text_content(live.prompt_name, "USER", heads_up, interactive=True):
                await self._send(live, ev)
        try:
            output = await run_tool("resolve_request", {"decision": decision}, self._tool_context())
        finally:
            self._pending_tools = max(0, self._pending_tools - 1)
        await self.emit(
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "name": "resolve_request",
                "output": output,
                "status": self._result_status(output, "resolve_request"),
            }
        )
        live = self._live
        if live is not None and not self._stopping:
            note = f"[Outcome of the request the user answered on screen:] {_for_sonic(output)}"
            for ev in protocol.text_content(live.prompt_name, "USER", note, interactive=True):
                await self._send(live, ev)
        await self._refresh_state()

    # ── input from the browser ───────────────────────────────────────────

    async def send_audio(self, pcm16: bytes) -> None:
        live = self._live
        if live is None or live.closing or not pcm16:
            return
        await self._send(
            live,
            protocol.audio_input(live.prompt_name, live.audio_name, pcm16),
            activity=_has_signal(pcm16),
        )

    async def send_text(self, text: str) -> None:
        clean = text.strip()
        if not clean:
            return
        live = await self._await_live()
        if live is None:
            return
        self._remember("user", clean)
        await self.emit({"type": "user_transcript", "text": clean, "typed": True})
        for ev in protocol.text_content(live.prompt_name, "USER", clean, interactive=True):
            await self._send(live, ev)

    # ── stream management ────────────────────────────────────────────────

    async def _open(self, *, first: bool) -> None:
        opener = self._open_stream or open_stream
        stream = await opener(self.config.model_id, self.config.region)
        if self._stopping:
            # The user hung up while the stream was opening: nobody would
            # close this one otherwise.
            with contextlib.suppress(Exception):
                await stream.close()
            return
        prompt_name = protocol.new_id()
        audio_name = protocol.new_id()
        now = self._clock()
        live = _Live(
            stream=stream,
            prompt_name=prompt_name,
            audio_name=audio_name,
            opened_at=now,
            last_input_at=now,
        )
        preamble = [
            protocol.session_start(
                max_tokens=self.config.max_tokens, endpointing=self.config.endpointing
            ),
            protocol.prompt_start(prompt_name, voice_id=self.config.voice_id, tools=VOICE_TOOLS),
            *protocol.text_content(prompt_name, "SYSTEM", self.config.system_prompt),
            *protocol.history_events(prompt_name, self.transcript, limit=TRANSCRIPT_LIMIT),
        ]
        if not first:
            # The transcript carries the words, not the tool state: without this
            # a renewed stream forgets that a run is still going or that it asked
            # the user a question, and answers from imagination.
            note = self._reconnect_note()
            if note:
                preamble.extend(protocol.text_content(prompt_name, "USER", note))
        preamble.append(protocol.audio_content_start(prompt_name, audio_name))
        for ev in preamble:
            await stream.send(ev)
        self._live = live
        live.pump = asyncio.create_task(self._pump(live))
        if not first:
            await self.emit({"type": "renewed"})

    def _reconnect_note(self) -> str:
        """What a renewed stream must know that the transcript does not say."""
        parts: list[str] = []
        if self.runs:
            labels = "; ".join(str(info.get("request", "")) for info in self.runs.values())
            parts.append(
                f"{len(self.runs)} request(s) are still running in the background ({labels}); "
                "their results arrive later as messages starting with [Result of the request "
                "...]. Do not call ask_assistant again for them and do not say they are done."
            )
        if self.pending:
            what = (
                self.pending.get("summary")
                or self.pending.get("question")
                or self.pending.get("action")
                or "a decision"
            )
            parts.append(
                f"The assistant is waiting for the user's decision on: {what}. If the user "
                "answers (yes, no, or an answer), call resolve_request with it right away."
            )
        if not parts:
            return ""
        return "[The conversation continues after a connection renewal. " + " ".join(parts) + "]"

    async def _close_live(self, live: _Live, *, graceful: bool) -> None:
        live.closing = True
        if graceful:
            for ev in (
                protocol.content_end(live.prompt_name, live.audio_name),
                protocol.prompt_end(live.prompt_name),
                protocol.session_end(),
            ):
                try:
                    await live.stream.send(ev)
                except Exception:  # noqa: BLE001 - stream may already be gone
                    break
        try:
            await live.stream.close()
        except Exception:  # noqa: BLE001
            pass
        if live.pump and live.pump is not asyncio.current_task():
            live.pump.cancel()
            try:
                await live.pump
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _send(self, live: _Live, event_json: str, *, activity: bool = True) -> None:
        """``activity`` says whether Sonic counts this event as input (text, tool
        results, audio with signal); silent audio does not reset its idle cut."""
        async with self._send_lock:
            if live.closing:
                return
            try:
                await live.stream.send(event_json)
            except Exception as exc:  # noqa: BLE001
                log.warning("voice: send failed, reopening stream: %s", exc)
                await self._reopen(live, why="send failed")
                return
            if activity:
                live.last_input_at = self._clock()

    async def _renew_loop(self) -> None:
        """Swap streams before Bedrock's 8-minute cut, and before Sonic's idle
        cut when the input has been silent, at a quiet moment."""
        try:
            while not self._stopping:
                await asyncio.sleep(1.0)
                live = self._live
                if live is None or live.closing:
                    continue
                now = self._clock()
                age = now - live.opened_at
                if age >= REOPEN_RESET_S and self._reopens:
                    self._reopens = 0
                quiet = not self._speaking and self._pending_tools == 0
                if age >= RENEW_AFTER_S and (quiet or age >= RENEW_DEADLINE_S):
                    await self._reopen(live, why="8-minute renewal")
                    continue
                idle = now - live.last_input_at
                if idle >= IDLE_RENEW_S and (quiet or idle >= IDLE_DEADLINE_S):
                    await self._reopen(live, why="idle input keepalive")
        except asyncio.CancelledError:
            pass

    async def _reopen(self, old: _Live, *, why: str) -> None:
        if self._stopping or old is not self._live or old.closing:
            return
        if self._reopens >= MAX_REOPENS:
            detail = f" ({self._last_stream_error})" if self._last_stream_error else ""
            await self._fail(
                f"Voice stream could not be renewed{detail}. Please start voice mode again."
            )
            return
        self._reopens += 1
        old.closing = True
        log.info("voice: reopening stream (%s)", why)
        await self.emit({"type": "renewing"})
        # Whatever the old stream was in the middle of is over: commit the
        # utterance, drop half-open blocks, stop showing "speaking".
        await self._end_utterance("renewal")
        self._spec_buf.clear()
        self._final_buf.clear()
        try:
            await self._open(first=False)
        except Exception as exc:  # noqa: BLE001
            await self._fail(f"Voice stream could not be reopened: {exc}")
            return
        finally:
            await self._close_live(old, graceful=False)

    async def _fail(self, message: str) -> None:
        log.error("voice: %s", message)
        await self.emit({"type": "error", "message": message})
        await self.stop("error")

    # ── output pump ──────────────────────────────────────────────────────

    async def _pump(self, live: _Live) -> None:
        parser = protocol.OutputParser()
        while True:
            try:
                raw = await live.stream.receive()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001 - SonicStreamError or transport
                # A failed stream is not a failed conversation: reopen with the
                # transcript so far (background runs and the browser keep going).
                # Persistent failures exhaust the reopen budget and end it.
                if live is self._live and not self._stopping and not live.closing:
                    await self._stream_failed(live, f"{type(exc).__name__}: {exc}")
                return
            if raw is None:
                break
            try:
                parsed = parser.parse(raw)
            except (AttributeError, TypeError, KeyError, ValueError) as exc:
                # One odd field is not worth a stream: skip the event.
                log.warning("voice: ignoring malformed model event: %s", exc)
                continue
            if parsed:
                live.events += len(parsed)
                self._ever_healthy = True
            for ev in parsed:
                try:
                    await self._handle(ev, live)
                except asyncio.CancelledError:
                    return
                except Exception:  # noqa: BLE001 - a bug here must not cost the stream
                    log.exception("voice: event handler failed for %s", ev.get("kind"))
        # The model closed the stream on its own (the 8-minute cut, or an
        # error we did not see). If this is still the live stream, reopen.
        if live is self._live and not self._stopping and not live.closing:
            await self._stream_failed(live, "the model closed the stream before responding")

    async def _stream_failed(self, live: _Live, error: str) -> None:
        """A live stream died. A stream that never delivered a model event on
        a conversation that never had one is not a hiccup to retry: it is the
        first request being refused (model not enabled in the region, no
        access, a transport that cannot reach Bedrock), so the user gets that
        error now instead of a renewal message minutes later."""
        self._last_stream_error = error
        if not self._ever_healthy and live.events == 0:
            await self._fail(
                f"Voice could not start: {error}. Check that {self.config.model_id} "
                f"is available to this account in {self.config.region}."
            )
            return
        log.warning("voice: stream failed (%s); reopening", error)
        await self._reopen(live, why=f"stream failed: {error}")

    async def _handle(self, ev: dict[str, Any], live: _Live) -> None:
        kind = ev["kind"]
        if kind == "user_transcript":
            text = ev["text"].strip()
            if text:
                await self._end_utterance("user")
                self._remember("user", text)
                await self.emit({"type": "user_transcript", "text": text, "typed": False})
        elif kind == "assistant_text":
            cid = ev.get("content_id", "")
            if _is_control_marker(ev["text"]):
                # Observed live: after a barge-in Sonic emits a FINAL text block
                # holding the literal marker {"interrupted": true}. It is a
                # signal, not speech; never show or remember it.
                return
            if ev.get("stage") == "final":
                self._final_buf.setdefault(cid, []).append(ev["text"])
            else:
                # Observed live: one response arrives as SEVERAL speculative
                # blocks (one per sentence) interleaved with its audio, so a new
                # block never ends the utterance; only completion boundaries,
                # user speech, an interruption or the quiet timer do.
                utt = self._utterance()
                utt.spec_cids.add(cid)
                self._spec_buf.setdefault(cid, []).append(ev["text"])
                utt.spec_parts.append(ev["text"])
                self._touch(utt)
                await self.emit(
                    {
                        "type": "assistant_text",
                        "text": ev["text"],
                        "final": False,
                        "utterance_id": utt.id,
                    }
                )
                await self._set_speaking(True)
        elif kind == "assistant_text_end":
            cid = ev.get("content_id", "")
            if ev.get("stage") == "final":
                text = " ".join(t.strip() for t in self._final_buf.pop(cid, []) if t.strip())
                if text:
                    if self._utt is None and self._is_late_confirmation():
                        # The utterance already committed (its preview, which is
                        # what the user heard): a straggling confirmation is not
                        # new speech, and Sonic never confirms without a preview.
                        return
                    # A confirmed chunk: the speech is still going (or just
                    # ended); the utterance commits once its speech has ended.
                    utt = self._utterance()
                    utt.final_parts.append(text)
                    self._touch(utt)
            else:
                self._spec_buf.pop(cid, None)
        elif kind == "audio":
            utt = self._utterance()
            if utt.first_audio_at is None:
                utt.first_audio_at = self._clock()
            utt.audio_bytes += len(ev["pcm"])
            self._touch(utt)
            await self._set_speaking(True)
            await self.emit({"type": "audio", "pcm": ev["pcm"]})
        elif kind == "interrupted":
            await self.emit({"type": "interrupted"})
            utt = self._utt
            if utt is not None:
                # The user cut in: the trailing FINAL chunks say what was
                # actually said; give them a moment, then commit.
                utt.interrupted_at = self._clock()
                self._arm_timer(utt)
            await self._set_speaking(False)
        elif kind == "tool_use":
            live.tool_ids.add(ev["tool_use_id"])
            await self.emit(
                {
                    "type": "tool_call",
                    "tool_use_id": ev["tool_use_id"],
                    "name": ev["name"],
                    "input": ev["input"],
                }
            )
            task = asyncio.create_task(self._run_tool(ev, live))
            self._tool_tasks.add(task)
            task.add_done_callback(self._tool_tasks.discard)
        elif kind == "turn_end":
            await self._end_utterance("turn_end")
        elif kind == "turn_start":
            await self._end_utterance("turn_start")
        elif kind == "usage":
            await self.emit({"type": "usage", **{k: v for k, v in ev.items() if k != "kind"}})
        elif kind == "error":
            await self.emit({"type": "error", "message": ev.get("message", "stream error")})

    # ── utterances ───────────────────────────────────────────────────────

    def _utterance(self) -> _Utterance:
        if self._utt is None:
            now = self._clock()
            self._utt = _Utterance(id=protocol.new_id()[:8], started_at=now, last_event_at=now)
        return self._utt

    def _touch(self, utt: _Utterance) -> None:
        utt.last_event_at = self._clock()
        self._arm_timer(utt)

    def _arm_timer(self, utt: _Utterance) -> None:
        """(Re)schedule the quiet commit: after the speech has ended plus a
        margin, capped after the last event; a short grace after a barge-in."""
        if utt.timer is not None:
            utt.timer.cancel()
        now = self._clock()
        if utt.interrupted_at is not None:
            deadline = utt.interrupted_at + INTERRUPT_GRACE_S
        else:
            deadline = min(
                max(utt.speech_end(now), utt.last_event_at) + UTTERANCE_QUIET_S,
                utt.last_event_at + UTTERANCE_MAX_WAIT_S,
            )
        delay = max(0.01, deadline - now)

        async def _later() -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            if self._utt is utt:
                await self._end_utterance("quiet")

        utt.timer = asyncio.create_task(_later())

    def _is_late_confirmation(self) -> bool:
        return self._clock() - self._last_commit_at <= LATE_FINAL_WINDOW_S

    async def _end_utterance(self, reason: str) -> None:
        """Commit the utterance in progress exactly once: the confirmed text
        when it is complete, otherwise the preview the user heard."""
        utt = self._utt
        if utt is None:
            return
        self._utt = None
        if utt.timer is not None:
            utt.timer.cancel()
        finals = " ".join(p.strip() for p in utt.final_parts if p.strip())
        spec = " ".join(p.strip() for p in utt.spec_parts if p.strip())
        complete = utt.interrupted_at is not None or not spec
        complete = complete or len(finals) >= CONFIRMED_COMPLETE_RATIO * len(spec)
        text = finals if finals and complete else (spec or finals)
        if text:
            log.debug("voice: utterance %s committed (%s)", utt.id, reason)
        await self._commit_assistant(text, utt.id)
        await self._set_speaking(False)

    async def _commit_assistant(self, text: str, utterance_id: str) -> None:
        clean = " ".join(text.split())
        if not clean:
            return
        self._remember("assistant", clean)
        self._last_committed = _normalized(clean)
        self._last_commit_at = self._clock()
        await self.emit(
            {"type": "assistant_text", "text": clean, "final": True, "utterance_id": utterance_id}
        )

    async def _run_tool(self, ev: dict[str, Any], live: _Live) -> None:
        self._pending_tools += 1
        await self._refresh_state()
        try:
            output = await run_tool(ev["name"], ev["input"], self._tool_context())
        except asyncio.CancelledError:
            return
        finally:
            self._pending_tools = max(0, self._pending_tools - 1)
        await self.emit(
            {
                "type": "tool_result",
                "tool_use_id": ev["tool_use_id"],
                "name": ev["name"],
                "output": output,
                "status": self._result_status(output, ev["name"]),
            }
        )
        current = await self._await_live()
        if current is None:
            if not self._stopping:
                log.warning("voice: no live stream to hand the %s result to", ev["name"])
            return
        spoken = _for_sonic(output)
        if current is live and not live.closing:
            for out_ev in protocol.tool_result(live.prompt_name, ev["tool_use_id"], spoken):
                await self._send(live, out_ev)
        else:
            # The stream was renewed while the tool ran: the new session never
            # saw the toolUse, so deliver the result as a typed turn instead.
            note = f"[Result of your earlier {ev['name']} request] {spoken}"
            for out_ev in protocol.text_content(
                current.prompt_name, "USER", note, interactive=True
            ):
                await self._send(current, out_ev)
        await self._refresh_state()

    # ── bookkeeping ──────────────────────────────────────────────────────

    def _remember(self, role: str, content: str) -> None:
        # Sonic hands over speech in pieces (a USER block per utterance, a
        # FINAL assistant block per sentence). Consecutive same-role pieces are
        # one turn, so merge them: the renewal seed stays role-alternating and
        # matches the single bubble the browser shows.
        if self.transcript and self.transcript[-1]["role"] == role:
            self.transcript[-1]["content"] = f"{self.transcript[-1]['content']} {content}".strip()
            return
        self.transcript.append({"role": role, "content": content})
        if len(self.transcript) > TRANSCRIPT_LIMIT:
            del self.transcript[: len(self.transcript) - TRANSCRIPT_LIMIT]

    async def _set_speaking(self, speaking: bool) -> None:
        if speaking != self._speaking:
            self._speaking = speaking
            await self._refresh_state()

    async def _refresh_state(self) -> None:
        if self._speaking:
            state = "speaking"
        elif self._pending_tools > 0:
            state = "thinking"
        else:
            state = "listening"
        await self._set_state(state)

    async def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            await self.emit({"type": "state", "state": state})


# int16 peak below which a frame counts as silence for Sonic's idle cut: a quiet
# room's microphone noise floor sits well under this, speech far above.
_SIGNAL_PEAK = 300


def _normalized(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


def _has_signal(pcm16: bytes) -> bool:
    """True when the frame carries audible signal (not digital silence and not
    a quiet room's noise floor). Sampled every few samples: speech has plenty."""
    usable = len(pcm16) - (len(pcm16) % 2)
    if usable < 2:
        return False
    samples = array("h", pcm16[:usable])
    step = max(1, len(samples) // 80)
    return any(abs(v) >= _SIGNAL_PEAK for v in samples[::step])


def _is_control_marker(text: str) -> bool:
    """True for Sonic's in-band JSON markers (e.g. ``{"interrupted": true}``)
    that arrive as assistant text but are not words it spoke."""
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return False
    try:
        data = json.loads(stripped)
    except ValueError:
        return False
    return isinstance(data, dict) and "interrupted" in data


def _for_sonic(text: str) -> str:
    """Trim a tool result for the speech model; the UI already has the full text."""
    if len(text) <= SPOKEN_RESULT_CHARS:
        return text
    return text[: SPOKEN_RESULT_CHARS - 1] + "… (truncated; the full answer is shown on screen)"


__all__ = ["VoiceConfig", "VoiceSession", "build_system_prompt", "SonicUnavailable"]
