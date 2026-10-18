"""Model-written captions for the activity rows of a chat turn.

The browser draws each run of tool calls as one activity row
(src/components/chat/ActivityRow.tsx) and names it with a deterministic
sentence ("Read 3 files"). When a round's tool phase has passed the safety
gate, ``start_label_task`` asks the auxiliary model of the ``activity_label``
task (Haiku unless ``auxiliary_models`` names another) for a short caption of
what the round achieved. The runner sends it as an ``activity_label`` frame
keyed by the round's first labelled tool_use_id, and the browser puts it on
the group that holds that call: the caption becomes the label and the
deterministic sentence becomes its tail. Captions are made per round, so a
group whose calls come from several rounds shows the last round's caption.

A caption is decoration and never costs the turn anything. The call runs on
its own two worker threads (not the loop's default executor, which session
loads and saves share) under a cap of LABEL_TIMEOUT_S, and a refusal, an
error or a timeout gives None, so the deterministic label stays. No error
reaches the user. A call that the cap abandoned still finishes in its
thread, and it is billed and logged under the ``activity_label`` source, the
same as an abandoned permission explainer call. While a session's earlier
call is still in its thread, that session starts no new one, so a hung model
holds at most one thread per session. A Claude key runs on the captions' own
bedrock-runtime client (a short read timeout, no retry): botocore keeps one
adaptive rate limiter per client, and throttled captions on the shared chat
client would slow the chat's own requests.

No caption is asked for:

- outside the user-facing chat turn (cost_source "chat"): agents,
  workflows, cron, voice, wake and headless runs never ask;
- in Local mode: the ``activity_labels`` flag is off there, and aux_refusal
  names the cloud guard's reason;
- when the task resolves to an on-device model (mapped to a local key, or to
  "main" with an on-device chat model): a generation per round would compete
  with the turn's own stream on the same machine;
- for a round that paused for an approval or a question: the tool phase
  returns before its caption starts, and the continuation does not caption
  the resumed calls, so their group keeps the deterministic label.

``PendingLabels`` holds the turn's label tasks in round order. The runner
sends the finished ones as the next round streams (one done() check for each
provider event), always in round order, and at the end of the turn it waits
for the rest, with the same cap, before [DONE]. A turn that paused does not
wait: it sends what landed and drops the rest. A turn with no label task ends
exactly as before. The frames carry no prose and the model never reads them,
so the claim guard and the persisted reminders are not affected.
"""

import asyncio
import concurrent.futures
import logging
import re
import threading
import unicodedata

from server.infrastructure.auxiliary import aux_one_shot, aux_refusal, resolve_task_key
from server.infrastructure.feature_flags import is_enabled
from server.utils import ndjson_dumps

from .context import TurnContext

log = logging.getLogger("whisper-studio")

TASK = "activity_label"
FLAG = "activity_labels"
# The longest a caption may take: from the end of its round, and again at the
# end of the turn for the captions still running.
LABEL_TIMEOUT_S = 3.0
MAX_TOKENS = 24
MAX_LABEL_CHARS = 80
# How much of each call the model reads: the head of the output the gate
# passed, and the call's subject (a path, a command, a pattern).
OUTPUT_HEAD_CHARS = 200
SUBJECT_CHARS = 120
# The captions' own client: how long a call the cap abandoned can still hold
# its thread before the client gives up.
CLIENT_READ_TIMEOUT_S = 10
CLIENT_CONNECT_TIMEOUT_S = 5

SYSTEM = (
    "You caption what an assistant just did with its tools. Reply with one caption "
    "of at most eight words, sentence case, no final period, no quotes, no emoji. "
    "Name the subject (a file, a test, a command) when it helps. Say what was "
    "achieved, not which tools were used."
)

# Calls the browser never draws in an activity row (src/components/chat/
# AgentCard.tsx: AGENT_GROUP_TOOLS, TASK_CARD_TOOLS and ACTIVITY_EXEMPT_TOOLS):
# the agent cards, the task list and the interactive cards. A caption keyed by
# one of them would find no group to name.
_SKIP_NAMES = frozenset(
    {
        "spawn_agent",
        "send_message",
        "list_agents",
        "ask_user_question",
        "ws_workspace_prompt",
        "ws_open_folder",
        "create_artifact",
        "preview_screenshot",
    }
)
_SKIP_PREFIXES = ("team_", "task_", "workflow_", "ci_")

# The input keys that name a call's subject, most telling first.
_SUBJECT_KEYS = ("path", "file_path", "command", "pattern", "query", "url", "branch", "name")

_QUOTES = "\"'`\u201c\u201d\u2018\u2019"
_TRAILING = ". \u2026" + _QUOTES
_EM_DASH = re.compile(r"\s*\u2014\s*")
# Markdown emphasis the row would show as raw text: **bold**, and *italic*
# or _italic_ around a word only, so a glob (*.py), a snake_case name and
# __init__.py keep their characters. Every backtick is removed.
_EMPHASIS = (
    re.compile(r"\*\*(.+?)\*\*"),
    re.compile(r"(?<![\w*])\*(?=[^\W_])(.+?)(?<=[^\W_])\*(?![\w*]|\.\w)"),
    re.compile(r"(?<![\w_])_(?=[^\W_])(.+?)(?<=[^\W_])_(?![\w_]|\.\w)"),
)
# Glyphs a caption must not carry into the chat chrome: emoji and other
# symbols (So), modifier symbols and emoji skin tones (Sk), the arrows (Sm,
# so they are named here), and the joiners and selectors emoji come with.
_ARROWS = ((0x2190, 0x21FF), (0x27F0, 0x27FF), (0x2900, 0x297F), (0x2B00, 0x2BFF))
_EMOJI_JOINERS = frozenset("\u200d\ufe0e\ufe0f\u20e3")

# Captions run here, never on the loop's default executor: an abandoned call
# keeps its thread until the client gives up, and the default pool serves the
# session loads and saves.
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="activity-label"
)
# Each session's latest call, so a session starts no new caption while its
# earlier call is still in its thread. Read and written on the event loop.
_in_thread: dict[str, concurrent.futures.Future] = {}

_CLIENTS: dict[str, object] = {}
_CLIENT_LOCK = threading.Lock()


def _caption_client():
    """The captions' own bedrock-runtime client in the region from Settings:
    a short read timeout and no retry, so an abandoned call frees its thread
    soon and a throttled caption never slows the shared chat client (botocore
    keeps one adaptive rate limiter per client). Cached once credentials
    resolve (server/infrastructure/aws_clients.py)."""
    from botocore.config import Config as BotoConfig

    from server.infrastructure.aws_clients import cached_client
    from server.infrastructure.config import load_config

    region = load_config().get("bedrock_region", "us-east-1")
    return cached_client(
        _CLIENTS,
        _CLIENT_LOCK,
        region,
        "bedrock-runtime",
        region_name=region,
        config=BotoConfig(
            read_timeout=CLIENT_READ_TIMEOUT_S,
            connect_timeout=CLIENT_CONNECT_TIMEOUT_S,
            retries={"total_max_attempts": 1, "mode": "standard"},
        ),
    )


def _reset_caption_client_cache() -> None:
    """Test hook: drop the cached client so a patched boto3 takes effect
    (tests/conftest.py resets it between tests, like the chat client)."""
    with _CLIENT_LOCK:
        _CLIENTS.clear()


def _labelled(name: str) -> bool:
    return name not in _SKIP_NAMES and not name.startswith(_SKIP_PREFIXES)


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _subject(tool_input) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in _SUBJECT_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return _one_line(value)[:SUBJECT_CHARS]
    return ""


def _text(content) -> str:
    """A tool_result's content as text: a string as is, the text blocks of a
    block list (a screenshot's caption)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return "" if content is None else str(content)


def _calls(states: list, tool_results: list) -> list[tuple[str, str]]:
    """(tool_use_id, prompt line) for each call of the round a row can show.
    The output is the result the gate passed to the model (an inline-approved
    write's own result, not its approval sentinel), read on its head only."""
    from server.tool_executor import tool_outcome

    passed = {r.get("tool_use_id"): r.get("content") for r in tool_results if isinstance(r, dict)}
    calls = []
    for state in states:
        tool_use = state.tool_use
        name, tool_id = tool_use.get("name") or "", tool_use.get("id") or ""
        if not tool_id or not _labelled(name):
            continue
        output = _text(passed.get(tool_id, state.output))
        failed = " (failed)" if tool_outcome(name, output).is_error else ""
        head = _one_line(output[:OUTPUT_HEAD_CHARS])
        verb_subject = f"{name} {_subject(tool_use.get('input'))}".strip()
        calls.append((tool_id, f"{verb_subject}{failed}: {head}"))
    return calls


def _glyph(ch: str) -> bool:
    if ch in _EMOJI_JOINERS or unicodedata.category(ch) in ("So", "Sk"):
        return True
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _ARROWS)


def clean_label(reply) -> str | None:
    """The caption as a row shows it: one line of plain text (no emoji, no
    symbol glyph, no markdown emphasis), no surrounding quotes, no final
    period, no em dash, at most MAX_LABEL_CHARS (cut at a word). None when
    nothing is left."""
    text = "".join(ch for ch in str(reply or "") if not _glyph(ch))
    for pattern in _EMPHASIS:
        text = pattern.sub(r"\1", text)
    text = _EM_DASH.sub(", ", _one_line(text.replace("`", "").replace("**", "")))
    text = text.strip(_QUOTES + " ").rstrip(_TRAILING)
    if len(text) > MAX_LABEL_CHARS:
        cut = text[:MAX_LABEL_CHARS]
        text = cut.rsplit(" ", 1)[0] if " " in cut else cut
        text = text.rstrip(" ,;:").rstrip(_TRAILING)
    return text or None


def _on_device(model_key: str) -> bool:
    """Whether the task resolves to an on-device model for this session."""
    from server.local.runtime import is_local_model

    return is_local_model(resolve_task_key(TASK, "haiku", model_key))


async def _caption(future: concurrent.futures.Future, ids: list[str]) -> dict | None:
    """The activity_label payload for one round, or None when the model gave
    nothing usable in time. Never raises into the turn; a cancel (the turn
    ended or was stopped) still cancels it."""
    try:
        reply = await asyncio.wait_for(asyncio.wrap_future(future), LABEL_TIMEOUT_S)
    except asyncio.TimeoutError:
        log.debug(
            "Activity label timed out (%.1f s); the deterministic label stays", LABEL_TIMEOUT_S
        )
        return None
    except Exception as e:  # noqa: BLE001 - a caption never costs the turn
        log.debug("Activity label failed (%s); the deterministic label stays", e)
        return None
    label = clean_label(reply)
    if label is None:
        return None
    return {"tool_use_id": ids[0], "tool_use_ids": ids, "label": label}


def start_label_task(ctx: TurnContext, states: list, tool_results: list) -> asyncio.Task | None:
    """Start the caption of a round whose tool phase passed the gate, or
    return None when this round gets none: not the chat turn, no call a row
    shows, the flag off, the task refused here (Local mode), the task on an
    on-device model, or the session's earlier call still in its thread."""
    try:
        if ctx.cost_source != "chat":
            return None
        calls = _calls(states, tool_results)
        if not calls or not is_enabled(FLAG):
            return None
        if _on_device(ctx.model_key) or aux_refusal(TASK, main_model_key=ctx.model_key):
            return None
        earlier = _in_thread.get(ctx.session_id)
        if earlier is not None and not earlier.done():
            log.debug("Activity label skipped: the earlier one is still in its thread")
            return None
        future = _EXECUTOR.submit(
            aux_one_shot,
            TASK,
            SYSTEM,
            "\n".join(line for _, line in calls),
            max_tokens=MAX_TOKENS,
            main_model_key=ctx.model_key,
            session_id=ctx.session_id,
            get_bedrock_client=_caption_client,
        )
    except Exception as e:  # noqa: BLE001 - a caption never costs the turn
        log.debug("Activity label not started: %s", e)
        return None
    _in_thread[ctx.session_id] = future
    task = asyncio.create_task(
        _caption(future, [tool_id for tool_id, _ in calls]), name="activity-label"
    )
    # A call still queued when nobody waits for it any more never starts.
    task.add_done_callback(lambda _task: future.cancel())
    return task


def _frames(tasks: list[asyncio.Task]) -> list[str]:
    frames = []
    for task in tasks:
        if not task.done() or task.cancelled() or task.exception() is not None:
            continue
        payload = task.result()
        if payload:
            frames.append(f"data: {ndjson_dumps({'activity_label': payload})}\n\n")
    return frames


class PendingLabels:
    """The turn's caption tasks, in round order. Frames leave in that order
    too, so a group that gets a caption from several rounds ends with the
    last one: a caption that landed waits for the earlier rounds' captions,
    and each of those waits at most LABEL_TIMEOUT_S."""

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []

    def add(self, task: asyncio.Task | None) -> None:
        if task is not None:
            self._tasks.append(task)

    def ready(self) -> list[str]:
        """The frames of the captions that have landed, without a wait: the
        leading run of finished tasks, so no frame overtakes an earlier
        round's."""
        landed = 0
        while landed < len(self._tasks) and self._tasks[landed].done():
            landed += 1
        if not landed:
            return []
        done, self._tasks = self._tasks[:landed], self._tasks[landed:]
        return _frames(done)

    def flush(self) -> list[str]:
        """At a pause for an approval or a question: the frames of the
        captions that have landed, in round order, and the rest cancelled,
        with no wait, so the card can be answered at once. A dropped caption
        never arrives, so a later round's caption sent here is still the last
        one for its group."""
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            if not task.done():
                task.cancel()
        return _frames(tasks)

    async def drain(self, timeout: float | None = None) -> list[str]:
        """At the end of the turn: wait up to ``timeout`` (LABEL_TIMEOUT_S)
        for the captions still running, return the frames of all that landed,
        in round order, and cancel the rest. No task, no wait."""
        if not self._tasks:
            return []
        running = [t for t in self._tasks if not t.done()]
        try:
            if running:
                await asyncio.wait(running, timeout=LABEL_TIMEOUT_S if timeout is None else timeout)
        finally:
            # Also on a Stop during the wait: no caption outlives the turn.
            tasks, self._tasks = self._tasks, []
            for task in tasks:
                if not task.done():
                    task.cancel()
        return _frames(tasks)

    def cancel(self) -> None:
        """Drop every caption still running (the turn was stopped or torn down)."""
        for task in self._tasks:
            task.cancel()
        self._tasks = []
