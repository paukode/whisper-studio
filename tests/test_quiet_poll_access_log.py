"""uvicorn access-log filter for pure UI polls: a successful poll is hidden,
but a failing one, and any non-GET, still logs. The preview poll alone was 75%
of a 221K-line backend log before this."""

import logging


def _rec(msg: str) -> logging.LogRecord:
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, msg, None, None)


def test_successful_polls_are_hidden_but_failures_and_writes_still_log():
    from server.main import _QuietPollAccessLog

    f = _QuietPollAccessLog()
    hidden = [
        '127.0.0.1:1 - "GET /api/preview/sessions HTTP/1.1" 200 OK',
        '127.0.0.1:1 - "GET /api/workspace/status HTTP/1.1" 200 OK',
        '127.0.0.1:1 - "GET /api/notifications/unread-count HTTP/1.1" 200 OK',
        '127.0.0.1:1 - "GET /api/workspace/index/status HTTP/1.1" 200 OK',
    ]
    kept = [
        '127.0.0.1:1 - "GET /api/preview/sessions HTTP/1.1" 500 Internal Server Error',
        '127.0.0.1:1 - "POST /api/preview/sessions HTTP/1.1" 200 OK',
        '127.0.0.1:1 - "DELETE /api/preview/sessions/p1 HTTP/1.1" 200 OK',
        '127.0.0.1:1 - "POST /api/chat HTTP/1.1" 200 OK',
    ]
    assert all(not f.filter(_rec(m)) for m in hidden)
    assert all(f.filter(_rec(m)) for m in kept)
