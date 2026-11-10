"""Convert an uploaded document in a child process that a cancel can kill.

A large PDF takes minutes to read into text (pdfminer is pure Python). In a
thread of the server it held the GIL for all of that time: while one 60,000
line PDF converted, every turn took 5 to 6 s to start instead of 0.01 s. A
thread also cannot be stopped, so a cancelled upload went on converting for
the whole 8 minutes. A child process gives the server its own interpreter
back and dies with the upload.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from collections.abc import Awaitable, Callable

from server.infrastructure.paths import repo_root

log = logging.getLogger("whisper-studio")

# How often the wait checks whether the client is still there.
_POLL_S = 0.5


def _worker_cmd(src: str, ext: str, filename: str, out: str) -> list[str]:
    """The child's command line. Module-level so tests can stub it."""
    return [sys.executable, "-m", "server.extract.worker", src, ext, filename, out]


async def convert_in_child(
    content: bytes,
    ext: str,
    filename: str,
    client_gone: Callable[[], Awaitable[bool]],
) -> str | None:
    """The document's text, or None when ``client_gone`` turned true first:
    the upload was cancelled, so the child is killed and nothing is kept.
    A conversion that fails returns the same ``[Error extracting ...]``
    marker convert_document writes."""
    with tempfile.TemporaryDirectory(prefix="ws-extract-") as tmp:
        src = os.path.join(tmp, "in" + ext)
        out = os.path.join(tmp, "out.txt")
        await asyncio.to_thread(_write, src, content)
        proc = await asyncio.create_subprocess_exec(
            *_worker_cmd(src, ext, filename, out),
            cwd=repo_root(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
        )
        exited = asyncio.ensure_future(proc.wait())
        try:
            while not exited.done():
                await asyncio.wait({exited}, timeout=_POLL_S)
                if not exited.done() and await client_gone():
                    log.info("Upload of %s cancelled; stopping its conversion", filename)
                    return None
            if proc.returncode != 0 or not os.path.exists(out):
                return f"[Error extracting {filename}: the conversion exited with code {proc.returncode}]"
            return await asyncio.to_thread(_read, out)
        finally:
            if proc.returncode is None:
                proc.kill()
            await exited


def _write(path: str, content: bytes) -> None:
    with open(path, "wb") as f:
        f.write(content)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()
