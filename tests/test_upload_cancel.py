"""A cancelled PDF upload stops its conversion, through the real server.

The disconnect has to cross the app's HTTP middleware: request.is_disconnected()
never saw it there, so the conversion of a cancelled upload ran to the end
(8 minutes for the field test's PDF). Run against uvicorn itself, with the
origin guard in front of the upload route, as in the app.
"""

import os
import socket
import sys
import threading
import time

import uvicorn
from fastapi import FastAPI

from server import attachments
from server.extract import child
from server.infrastructure.security import origin_guard


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_closing_the_upload_connection_kills_the_conversion(monkeypatch, tmp_path):
    pid_file = tmp_path / "pid"
    script = (
        f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    )
    monkeypatch.setattr(child, "_worker_cmd", lambda *a: [sys.executable, "-c", script])

    app = FastAPI()
    app.middleware("http")(origin_guard)
    app.include_router(attachments.router)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)

        boundary = "wsboundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; filename="big.pdf"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
            "%PDF-1.4 test\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        head = (
            "POST /api/upload HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Origin: http://127.0.0.1:{port}\r\n"
            f"Content-Type: multipart/form-data; boundary={boundary}\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode()
        conn = socket.create_connection(("127.0.0.1", port))
        conn.sendall(head + body)

        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        pid = int(pid_file.read_text())
        assert _alive(pid)

        conn.close()  # the user pressed Stop

        deadline = time.monotonic() + 5
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(pid)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
