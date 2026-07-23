"""WebSocket endpoints must reject cross-site handshakes.

The HTTP Origin/Host middleware does not see WebSocket upgrades, so /ws,
/ws/terminal, /ws/lsp, and /ws/preview/.../screencast each have to check the
Origin themselves. Without it a malicious page in the user's browser could open
a terminal PTY (shell/RCE), drive a language server, or read the preview stream
against the local app (WebSockets are not subject to CORS)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.lsp_proxy import router as lsp_router
from server.preview.screencast import router as screencast_router
from server.terminal import router as terminal_router

_EVIL = {"origin": "https://evil.example"}


def _client(router) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    # base_url localhost so the Host header itself is trusted; we are isolating
    # the Origin check on the WS handshake.
    return TestClient(app, base_url="http://localhost")


@pytest.mark.parametrize(
    "router,path",
    [
        (terminal_router, "/ws/terminal/sess-1"),
        (lsp_router, "/ws/lsp/python"),
        (screencast_router, "/ws/preview/demo/screencast"),
    ],
)
def test_ws_rejects_cross_site_origin(router, path):
    c = _client(router)
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(path, headers=_EVIL) as ws:
            ws.receive_text()
