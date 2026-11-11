"""server/extract/child.py: a document converts in a child process, and a
cancelled upload kills that process instead of converting for minutes."""

import asyncio
import os
import sys
import time

from server.extract import child


async def _never_gone() -> None:
    await asyncio.Event().wait()


def test_a_document_converts_in_the_child():
    text = asyncio.run(
        child.convert_in_child(b"# Notes\n\nThe sea is blue.\n", ".md", "notes.md", _never_gone())
    )

    assert "The sea is blue." in text


def test_a_cancelled_upload_kills_the_child(monkeypatch, tmp_path):
    pid_file = tmp_path / "pid"
    script = (
        f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    )
    monkeypatch.setattr(child, "_worker_cmd", lambda *a: [sys.executable, "-c", script])

    async def gone_once_started() -> None:
        while not pid_file.exists():
            await asyncio.sleep(0.05)

    started = time.monotonic()
    result = asyncio.run(child.convert_in_child(b"%PDF", ".pdf", "big.pdf", gone_once_started()))

    assert result is None
    assert time.monotonic() - started < 10
    pid = int(pid_file.read_text())
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive


def test_a_child_that_fails_gives_the_error_marker(monkeypatch):
    monkeypatch.setattr(
        child, "_worker_cmd", lambda *a: [sys.executable, "-c", "import sys; sys.exit(3)"]
    )

    text = asyncio.run(child.convert_in_child(b"%PDF", ".pdf", "big.pdf", _never_gone()))

    assert text == "[Error extracting big.pdf: the conversion exited with code 3]"
