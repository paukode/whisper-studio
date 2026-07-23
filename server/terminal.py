"""
Terminal WebSocket Proxy — bridges browser xterm.js ↔ real PTY sessions.

Each WebSocket connection manages one PTY (pseudo-terminal) running zsh.
Supports multiple concurrent sessions, resize, and clean shutdown.

Architecture:
  Every session has ONE canonical reader task that owns the master_fd
  reads. It appends to a per-session output buffer and notifies
  subscribers via asyncio.Event. WebSocket forwarders subscribe to
  the event for live streaming; the terminal_run marker watcher
  polls the buffer instead (no subscribe). This is what makes it
  safe to drive a single PTY from both the user (typing) and the
  assistant (terminal_run) at the same time.

Endpoints:
  WebSocket /ws/terminal/{session_id}   — PTY I/O for a session (input,
            output, and {type:'resize'} messages)
  POST      /api/terminal/create        — create a new session, returns session_id
  DELETE    /api/terminal/{session_id}   — kill a session
"""

import asyncio
import fcntl
import json
import logging
import os
import pty
import re
import secrets
import signal
import struct
import subprocess
import termios
import threading
import time
import uuid

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

log = logging.getLogger("whisper-studio")

router = APIRouter()

# Active PTY sessions: session_id -> _PtySession. Guarded by _sessions_lock
# (threading.Lock, not asyncio, so it stays correct if a worker thread ever
# touches the dict). Held only for the dict op / snapshot — never during a
# syscall, kill(), or await.
_sessions: dict[str, "_PtySession"] = {}
_sessions_lock = threading.Lock()

# Per-session output buffer cap. Past this the oldest bytes are dropped.
# 1 MB comfortably holds the output of any non-pathological command;
# WebSocket subscribers drain in real time so this is essentially a
# safety valve against runaway output (e.g. `find /` on a fresh shell).
_OUTPUT_BUFFER_CAP = 1_048_576


class _PtySession:
    """Manages a single PTY subprocess.

    Output flow:
      master_fd → _pty_reader task → output_buffer (bytearray)
                                  ↘ output_event.set() — wakes WebSocket forwarders
      Marker watchers (run_in_session) don't subscribe — they poll
      the buffer at 50ms intervals using a recorded start position.
    """

    def __init__(
        self,
        session_id: str,
        master_fd: int,
        process: subprocess.Popen,
        cols: int,
        rows: int,
        cwd: str,
        hidden: bool = False,
        profile_path: str | None = None,
    ):
        self.id = session_id
        self.master_fd = master_fd
        self.process = process
        self.cols = cols
        self.rows = rows
        self.cwd = cwd
        # `hidden` sessions are sandbox PTYs spawned by terminal_run for
        # one-shot probes. They never get a WebSocket attached.
        self.hidden = hidden
        # Temp sandbox-exec profile backing a sandboxed hidden PTY (macOS);
        # unlinked in kill(). None when unsandboxed.
        self.profile_path = profile_path
        # Output capture machinery.
        self.output_buffer = bytearray()
        self.output_lock = threading.Lock()
        # Created lazily on first subscribe — needs an active event loop.
        self.output_event: asyncio.Event | None = None
        # Reader task handle, set by _start_reader().
        self._reader_task: asyncio.Task | None = None
        # Shutdown handshake: kill() sets this and hangs up the shell; the
        # reader notices it between blocking reads, exits, and closes
        # master_fd itself (see _pty_reader). Set-once bool, GIL-atomic.
        self._closing = False
        # Total bytes ever produced. Used so a poller can record an absolute
        # offset that survives buffer trims (rare in practice but defensive).
        self.bytes_produced = 0

    @property
    def alive(self) -> bool:
        return self.process.poll() is None

    def resize(self, cols: int, rows: int):
        self.cols = cols
        self.rows = rows
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

    def kill(self):
        # Signal shutdown and hang up the shell. We deliberately do NOT close
        # master_fd here: the reader may be blocked in select()/os.read() on it
        # right now, and closing it out from under that syscall races a recycled
        # fd (the number could be reissued to another session/socket before the
        # reader's read returns). Instead the reader owns the close — it exits
        # once it sees _closing (or the shell dies) and closes the fd after its
        # final read returns. See _pty_reader.
        had_reader = self._reader_task is not None
        self._closing = True
        try:
            os.kill(self.process.pid, signal.SIGHUP)
        except (ProcessLookupError, OSError):
            pass
        self._reader_task = None
        if not had_reader:
            # No reader was ever started (defensive): nobody else will close the
            # fd, so close it here. Normal sessions always have a reader.
            try:
                os.close(self.master_fd)
            except OSError:
                pass
        if self.profile_path:
            try:
                os.unlink(self.profile_path)
            except OSError:
                pass
            self.profile_path = None


async def _pty_reader(session: _PtySession) -> None:
    """Background task — single owner of master_fd reads AND of closing it.

    Appends bytes to session.output_buffer and wakes any output_event
    subscribers. Kept running for the entire session lifetime; exits when the
    shell dies or kill() sets session._closing. On exit it closes master_fd —
    always between reads, never mid-read — so kill() can hang up without racing
    an in-flight select()/read() on a possibly-recycled fd. Failures (OSError)
    terminate the task, not the process."""
    loop = asyncio.get_event_loop()
    try:
        while session.alive and not session._closing:
            try:
                data = await loop.run_in_executor(None, _blocking_read, session.master_fd)
            except (OSError, asyncio.CancelledError):
                break
            if not data:
                continue
            with session.output_lock:
                session.output_buffer.extend(data)
                session.bytes_produced += len(data)
                # Keep the buffer bounded so a misbehaving command can't OOM
                # the process. Drop oldest bytes when the cap is exceeded.
                if len(session.output_buffer) > _OUTPUT_BUFFER_CAP:
                    overflow = len(session.output_buffer) - (_OUTPUT_BUFFER_CAP // 2)
                    del session.output_buffer[:overflow]
            if session.output_event is not None:
                session.output_event.set()
    finally:
        # Sole owner of the close. Reached only after the last blocking read has
        # returned (the loop condition is checked between reads), so no thread
        # is touching master_fd when we close it.
        try:
            os.close(session.master_fd)
        except OSError:
            pass


def _ensure_event(session: _PtySession) -> asyncio.Event:
    """Lazily create the output_event on the calling loop. Safe to call
    repeatedly — returns the existing event after first call."""
    if session.output_event is None:
        session.output_event = asyncio.Event()
    return session.output_event


def _start_reader(session: _PtySession) -> None:
    """Spawn the per-session reader task. Idempotent."""
    if session._reader_task is None or session._reader_task.done():
        _ensure_event(session)
        session._reader_task = asyncio.create_task(_pty_reader(session))


def _child_acquire_ctty():
    """Child-side setup: new session + adopt the pty slave (already dup'd
    to fd 0) as the controlling terminal. Plain os.setsid leaves the child
    with NO controlling tty, so TIOCSWINSZ on the master updates the kernel
    winsize but SIGWINCH is delivered to the pty's (empty) foreground
    process group — the shell keeps its startup $COLUMNS/$LINES forever
    and never rewraps on resize."""
    os.setsid()
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    except OSError:
        # Degrade to the old no-ctty behavior (shell works, resize doesn't)
        # rather than failing the spawn — preexec exceptions abort Popen.
        pass


def _create_pty_session(
    cwd: str, cols: int = 80, rows: int = 24, hidden: bool = False
) -> _PtySession:
    """Spawn a new PTY session running the user's shell.

    `hidden=True` flags it as a sandbox session: omitted from the public
    sessions list and used only by terminal_run for one-shot commands.
    Either way, the reader task is started immediately so output capture
    works whether or not a WebSocket ever attaches.

    Visible sessions use the user's login shell (`zsh -l`) so the prompt
    looks normal. Sandbox sessions use a minimal shell with no rc files
    — predictable PATH, no theming, no plugin output mixed into command
    results. Falls back to /bin/sh if zsh/bash aren't available.
    """
    session_id = uuid.uuid4().hex[:12]

    master_fd, slave_fd = pty.openpty()

    # Set initial window size
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)

    if hidden:
        # Minimal, deterministic shell for sandbox runs. zsh -f / bash
        # --norc skip all per-user dotfiles so a fancy prompt theme can't
        # pollute command output.
        if os.path.exists("/bin/bash"):
            shell_cmd = ["/bin/bash", "--noprofile", "--norc"]
        elif os.path.exists("/bin/zsh"):
            shell_cmd = ["/bin/zsh", "-f"]
        else:
            shell_cmd = ["/bin/sh"]
    else:
        shell = os.environ.get("SHELL", "/bin/zsh")
        shell_cmd = [shell, "-l"]

    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    if hidden:
        # Plain terminal type for sandbox — no 256-color escapes, no fancy
        # OSC emitters. Some tools emit colored output only when TERM
        # advertises it; "dumb" forces plain text.
        env["TERM"] = "dumb"
        env.pop("COLORTERM", None)

    # Sandbox the hidden (terminal_run) PTY: run its shell under sandbox-exec
    # so commands typed into it inherit the same filesystem deny-list as the
    # workspace shell. Visible sessions are the user's own terminal and stay
    # unsandboxed. No-op off macOS (validation is still applied upstream).
    profile_path = None
    if hidden:
        from server.sandbox import build_pty_sandbox_wrap

        shell_cmd, profile_path = build_pty_sandbox_wrap(shell_cmd, cwd)

    process = subprocess.Popen(
        shell_cmd,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=cwd,
        env=env,
        preexec_fn=_child_acquire_ctty,
    )
    os.close(slave_fd)

    # Set master_fd to non-blocking
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    session = _PtySession(
        session_id, master_fd, process, cols, rows, cwd, hidden=hidden, profile_path=profile_path
    )
    with _sessions_lock:
        _sessions[session_id] = session
    _start_reader(session)
    log.info(
        "Terminal session created: %s (shell=%s, cwd=%s, hidden=%s)",
        session_id,
        " ".join(shell_cmd),
        cwd,
        hidden,
    )
    return session


# --- terminal_run helpers ------------------------------------------------
#
# Used by the terminal_run tool (server/executors/terminal.py). Two flows:
#   run_in_session(...)  — drive an EXISTING session (visible mode)
#   run_in_sandbox(...)  — spawn an ephemeral hidden session, run, kill
#
# Both return {"output": str, "exit_code": int, "timed_out": bool}.


_MAX_TIMEOUT_S = 300
_DEFAULT_TIMEOUT_S = 30

# Sequences we'd rather not surface to the model — they don't help it
# reason about the output and just inflate token count. Cursor moves,
# colour codes, OSC sequences (terminal title, prompt-marking, hyperlinks
# emitted by fancy prompts like powerlevel10k), and bare CRs (progress
# bars) all get stripped before returning to the LLM.
#
# Coverage:
#   CSI:  \x1b[ ... <letter>                          — colour + cursor
#   OSC:  \x1b] ... (BEL | ESC\)                      — title, hyperlinks
#   C1:   \x1b( or \x1b) or \x1b= or \x1b> or \x1b\\  — charset / keypad
#   bare: BEL (\x07), bare CR not followed by LF, backspace
_CSI_RE = re.compile(r"\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_C1_RE = re.compile(r"\x1b[\(\)=>\\(]")
_BEL_RE = re.compile(r"\x07")
_CR_RE = re.compile(r"\r(?!\n)")
_BS_RE = re.compile(r".\x08")  # backspace deletes prev char


def _strip_ansi(text: str) -> str:
    """Remove ANSI/OSC/C1 escape sequences + bare control characters.
    Keeps the output human-and-LLM readable without changing semantic
    content. Order matters: OSC before CSI because OSC swallows `]`."""
    text = _OSC_RE.sub("", text)
    text = _CSI_RE.sub("", text)
    text = _C1_RE.sub("", text)
    text = _BEL_RE.sub("", text)
    # Apply backspace removal until it stops shrinking — handles runs of
    # backspaces. Bounded by len(text) so this can't run away.
    for _ in range(64):
        new = _BS_RE.sub("", text)
        if new == text:
            break
        text = new
    text = _CR_RE.sub("", text)
    return text


def _clean_command_output(raw: str, command: str, marker: str) -> str:
    """Slice the marker line out, strip the echoed command, strip ANSI.
    Returns just the command's stdout/stderr, ready to hand to the model.
    """
    # Cut at the marker line (everything after is shell prompt noise).
    marker_re = re.compile(rf"__CLAUDE_DONE_{marker}:\d+")
    m = marker_re.search(raw)
    if m:
        raw = raw[: m.start()]
    # Strip ANSI + bare-CR control characters.
    cleaned = _strip_ansi(raw)
    # The shell typically echoes the command back as the first line(s).
    # Drop a leading line if it contains the literal command — best-effort,
    # don't over-trim if the command appears in a later line.
    lines = cleaned.split("\n")
    if lines and command.split("\n")[0].strip() and command.split("\n")[0].strip() in lines[0]:
        lines = lines[1:]
    return "\n".join(lines).strip("\r\n")


# How often to poll the output buffer for the completion marker. 50ms
# keeps perceived latency negligible while being far cheaper than
# wiring the marker watcher into the asyncio.Event subscriber list.
_MARKER_POLL_INTERVAL = 0.05


def _build_run_payload(command: str, marker: str) -> str:
    """Build the bytes written to the PTY to run ``command`` and print the
    completion marker with its exit code.

    The command and the marker printf go on SEPARATE input lines. Joining them
    with ``;`` on one line breaks when the command's last line ends in a ``#``
    comment (models emit these): the trailing ``; printf ...`` gets commented
    out, the marker never prints, and the poll loop hangs to the full timeout
    for a command that actually succeeded. Ending the command with a newline
    still lets ``$?`` on the printf line reflect the command's exit status. The
    leading ``\\n`` in the printf keeps the marker on its own line even when the
    command's output has no trailing newline.
    """
    return f"{command}\nprintf '\\n__CLAUDE_DONE_{marker}:%d\\n' $?\n"


async def run_in_session(
    session: _PtySession, command: str, timeout: float = _DEFAULT_TIMEOUT_S
) -> dict:
    """Send `command` into the session and capture stdout/stderr + exit
    code by waiting for a unique completion marker. Use for the visible
    mode of terminal_run (driving the user's existing terminal).

    Caveats:
      - Non-interactive only. Commands that wait for stdin (vim, less,
        password prompts) will hang to the timeout — the caller should
        guard against those upstream.
      - The user's typing happens in parallel; if they're mid-command
        when terminal_run fires, output interleaves. Acceptable trade-off
        for "the assistant uses my real terminal".
    """
    timeout = max(0.5, min(float(timeout), _MAX_TIMEOUT_S))
    marker = secrets.token_hex(8)
    marker_re = re.compile(rf"__CLAUDE_DONE_{marker}:(\d+)")

    with session.output_lock:
        start_pos = len(session.output_buffer)

    payload = _build_run_payload(command, marker)
    try:
        os.write(session.master_fd, payload.encode("utf-8"))
    except OSError as e:
        return {"output": f"failed to write to terminal: {e}", "exit_code": -1, "timed_out": False}

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(_MARKER_POLL_INTERVAL)
        with session.output_lock:
            text = bytes(session.output_buffer[start_pos:]).decode("utf-8", errors="replace")
        m = marker_re.search(text)
        if m:
            return {
                "output": _clean_command_output(text, command, marker),
                "exit_code": int(m.group(1)),
                "timed_out": False,
            }

    # Timed out — return whatever output accumulated, capped.
    with session.output_lock:
        text = bytes(session.output_buffer[start_pos:]).decode("utf-8", errors="replace")
    return {
        "output": _strip_ansi(text)[-8192:],
        "exit_code": -1,
        "timed_out": True,
    }


def latest_visible_session() -> _PtySession | None:
    """Return the most-recently-created live, non-hidden session, or None.
    Used by terminal_run(mode='visible') to pick which terminal to drive."""
    with _sessions_lock:
        snapshot = list(_sessions.values())
    live = [s for s in snapshot if s.alive and not s.hidden]
    if not live:
        return None
    return live[-1]


async def run_in_sandbox(
    command: str, cwd: str | None = None, timeout: float = _DEFAULT_TIMEOUT_S
) -> dict:
    """Spawn a fresh hidden PTY session, run `command`, kill the session.
    Use for the default (sandbox) mode of terminal_run. The session is
    invisible to the user and removed from `_sessions` on completion."""
    if not cwd:
        cwd = os.path.expanduser("~")
    if not os.path.isdir(cwd):
        return {"output": f"cwd not found: {cwd}", "exit_code": -1, "timed_out": False}

    session = _create_pty_session(cwd, hidden=True)
    try:
        # Give the shell a moment to print its banner / first prompt so
        # our command isn't lost in the warmup. Drain by recording the
        # buffer position AFTER a short settle delay.
        await asyncio.sleep(0.15)
        result = await run_in_session(session, command, timeout)
        return result
    finally:
        try:
            session.kill()
        except Exception:
            pass
        with _sessions_lock:
            _sessions.pop(session.id, None)


def _reap_dead_sessions() -> None:
    """Clean up dead sessions: pop them under the lock, then kill() outside it.
    kill() closes the fd + cancels the reader + unlinks the sandbox profile
    exactly once — no raw os.close/del that could KeyError or double-close a
    recycled fd. Runs on every session create so dead PTYs can't accumulate
    on a long-lived server."""
    with _sessions_lock:
        dead = [sid for sid, s in _sessions.items() if not s.alive]
        removed = [_sessions.pop(sid, None) for sid in dead]
    for s in removed:
        if s:
            s.kill()


# --- REST endpoints ---


@router.post("/api/terminal/create")
async def terminal_create(request: Request):
    body = await request.json()
    cwd = body.get("cwd", os.path.expanduser("~"))
    cols = body.get("cols", 80)
    rows = body.get("rows", 24)

    if not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")

    _reap_dead_sessions()
    session = _create_pty_session(cwd, cols, rows)
    return {"session_id": session.id, "cwd": session.cwd}


@router.delete("/api/terminal/{session_id}")
async def terminal_kill(session_id: str):
    with _sessions_lock:
        session = _sessions.pop(session_id, None)
    if not session:
        return Response(
            content=json.dumps({"error": "Session not found"}),
            status_code=404,
            media_type="application/json",
        )
    session.kill()
    log.info("Terminal session killed: %s", session_id)
    return {"killed": session_id}


# --- WebSocket PTY I/O ---


@router.websocket("/ws/terminal/{session_id}")
async def terminal_websocket(websocket: WebSocket, session_id: str):
    # Reject cross-site WebSocket handshakes (the HTTP Origin middleware does
    # not see WS upgrades). Without this a malicious page in the user's browser
    # could attach to a PTY and run shell commands on the machine.
    from server.infrastructure.security import is_ws_origin_allowed

    if not is_ws_origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=1008)
        return
    with _sessions_lock:
        session = _sessions.get(session_id)
    if not session or not session.alive or session.hidden:
        # Hidden sandbox sessions never accept user WS attaches — that
        # would defeat the "invisible to user" guarantee.
        await websocket.close(code=1008)
        return

    await websocket.accept()
    log.info("Terminal WebSocket connected: %s", session_id)

    # Subscribe to the centralised reader: every new chunk wakes us, we
    # drain from our position, forward. The reader owns the fd reads —
    # we never touch master_fd directly.
    event = _ensure_event(session)
    with session.output_lock:
        # Replay everything in the buffer on connect so reconnects don't
        # lose recent context (e.g. the previous prompt the user hasn't
        # seen yet).
        replay = bytes(session.output_buffer)
        pos = len(session.output_buffer)
    if replay:
        try:
            await websocket.send_text(replay.decode("utf-8", errors="replace"))
        except (WebSocketDisconnect, RuntimeError):
            return

    async def pty_to_ws():
        """Drain newly-buffered bytes to the WebSocket as they arrive."""
        nonlocal pos
        try:
            while session.alive:
                try:
                    await event.wait()
                except asyncio.CancelledError:
                    break
                # Clear BEFORE drain so any chunks that arrive during our
                # send don't get lost — they'll re-set the event and we'll
                # wake up immediately on the next wait().
                event.clear()
                with session.output_lock:
                    if pos > len(session.output_buffer):
                        # Buffer was trimmed; reset to current end.
                        pos = len(session.output_buffer)
                    new = bytes(session.output_buffer[pos:])
                    pos = len(session.output_buffer)
                if new:
                    await websocket.send_text(new.decode("utf-8", errors="replace"))
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass

    async def ws_to_pty():
        """Read from WebSocket, write to PTY master fd."""
        try:
            while True:
                msg = await websocket.receive_text()
                # Handle resize messages (JSON)
                if msg.startswith('{"type":"resize"'):
                    try:
                        parsed = json.loads(msg)
                        if parsed.get("type") == "resize":
                            session.resize(parsed.get("cols", 80), parsed.get("rows", 24))
                            continue
                    except (json.JSONDecodeError, KeyError):
                        pass
                # Regular input
                os.write(session.master_fd, msg.encode("utf-8"))
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except OSError:
            pass

    tasks = [
        asyncio.create_task(pty_to_ws()),
        asyncio.create_task(ws_to_pty()),
    ]

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    log.info("Terminal WebSocket disconnected: %s", session_id)


def _blocking_read(fd: int) -> bytes:
    """Blocking read from PTY fd (runs in executor thread)."""
    import select

    readable, _, _ = select.select([fd], [], [], 0.1)
    if readable:
        try:
            return os.read(fd, 4096)
        except OSError:
            return b""
    return b""
