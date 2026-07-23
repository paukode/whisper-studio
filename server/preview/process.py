"""Dev-server subprocess: validated, sandboxed, continuously log-drained.

Unlike terminal_run's spawn-capture-kill one-shot commands, a dev server must
stay alive across many tool calls. Spawned via plain asyncio subprocess (no
PTY needed) under the same sandbox wrapping terminal_run's long-lived PTY
sessions already use.
"""

from __future__ import annotations

import asyncio
import logging
import os

log = logging.getLogger("whisper-studio")

# Mirrors terminal.py's _OUTPUT_BUFFER_CAP — bounded ring buffer so a chatty
# dev server can't grow memory unbounded.
_OUTPUT_BUFFER_CAP = 1_048_576

# The exec itself is near-instant; this is a safety net against a pathological
# sandbox-wrap/exec hang so spawn never blocks the turn forever.
_SPAWN_TIMEOUT_S = 30


class DevServerSpawnError(Exception):
    """Command validation or spawn failure — surfaced to the model as text,
    no silent fallback."""


class DevServerProcess:
    """Wraps one long-lived asyncio subprocess with continuously drained,
    bounded stdout/stderr ring buffers."""

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        command: list[str],
        cwd: str,
        profile_path: str | None,
    ):
        self.proc = proc
        self.command = command
        self.cwd = cwd
        self._profile_path = profile_path
        self.stdout_buf = bytearray()
        self.stderr_buf = bytearray()
        self._lock = asyncio.Lock()
        self._reader_tasks: list[asyncio.Task] = [
            asyncio.create_task(self._drain(proc.stdout, "stdout_buf")),
            asyncio.create_task(self._drain(proc.stderr, "stderr_buf")),
        ]

    @classmethod
    async def spawn(
        cls, command: list[str], *, cwd: str, env: dict | None = None
    ) -> DevServerProcess:
        from server.sandbox import build_pty_sandbox_wrap
        from server.security.command_validator import validate_command

        # Validate the RAW joined argv, not a shell-quoted form: this process
        # is spawned via create_subprocess_exec (argv exec, no shell), so
        # quoting here would be misleading — worse, validate_command()
        # deliberately treats single-quoted spans as "shell-safe, no
        # expansion" and skips scanning them for sensitive paths, so a
        # shlex-quoted argv could hide a payload like ~/.ssh from detection
        # even though no shell is actually involved to make the quotes real.
        joined = " ".join(str(c) for c in command)
        warning = validate_command(joined)
        if warning:
            raise DevServerSpawnError(warning)

        if not os.path.isdir(cwd):
            raise DevServerSpawnError(f"cwd does not exist: {cwd}")

        argv, profile_path = build_pty_sandbox_wrap(command, cwd)
        try:
            # start_new_session=True (setsid), NOT preexec_fn=new_process_group.
            # This is the async, event-loop spawn — a preexec_fn callable forces
            # a fork() that then runs Python in the child, which under uvloop
            # deadlocks (a lock held at fork-time is never released in the child)
            # and wedges the whole event loop with a ~100% CPU forked child.
            # start_new_session does the equivalent setsid in C between fork and
            # exec (no Python in the child), and still makes the child a process
            # group leader so kill_process_group_async works unchanged.
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *argv,
                    cwd=cwd,
                    env={**os.environ, **(env or {})},
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                ),
                timeout=_SPAWN_TIMEOUT_S,
            )
        except asyncio.TimeoutError as e:
            if profile_path:
                try:
                    os.unlink(profile_path)
                except OSError:
                    pass
            raise DevServerSpawnError(f"process did not start within {_SPAWN_TIMEOUT_S}s") from e
        except Exception as e:  # noqa: BLE001
            if profile_path:
                try:
                    os.unlink(profile_path)
                except OSError:
                    pass
            raise DevServerSpawnError(f"failed to start process: {e}") from e
        return cls(proc, command, cwd, profile_path)

    async def _drain(self, stream: asyncio.StreamReader | None, attr: str):
        if stream is None:
            return
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            async with self._lock:
                buf = getattr(self, attr)
                buf.extend(chunk)
                if len(buf) > _OUTPUT_BUFFER_CAP:
                    del buf[: len(buf) - _OUTPUT_BUFFER_CAP // 2]

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None

    async def logs(self, *, stream: str = "both", tail_bytes: int = 8192) -> str:
        async with self._lock:
            parts = []
            if stream in ("both", "stdout"):
                parts.append(bytes(self.stdout_buf[-tail_bytes:]).decode("utf-8", "replace"))
            if stream in ("both", "stderr"):
                parts.append(bytes(self.stderr_buf[-tail_bytes:]).decode("utf-8", "replace"))
            return "\n".join(p for p in parts if p) or "(no output yet)"

    async def stop(self):
        from server.process_utils import kill_process_group_async

        for t in self._reader_tasks:
            t.cancel()
        await kill_process_group_async(self.proc)
        if self._profile_path:
            try:
                os.unlink(self._profile_path)
            except OSError:
                pass
