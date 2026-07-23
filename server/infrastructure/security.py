"""Origin / Host guard — the CSRF (and DNS-rebinding) defense for the local app.

Whisper Studio runs a fully-unauthenticated API bound to localhost. That is fine
for a single-user local tool *until* a web page you visit in your browser tries
to reach it: the browser will happily send a cross-origin POST to
``http://localhost:<port>/api/...`` (a CSRF attack), and some of those endpoints
execute shell commands. The browser blocks the attacker from *reading* the
response, but for state-changing requests the damage is already done.

The fix is to check the ``Origin`` (and ``Host``) headers on every request and
reject anything that did not come from the local app itself. A browser always
sets ``Origin`` to the *real* page origin and a script cannot forge it, so a page
served from ``https://evil.example`` cannot pretend to be ``http://localhost``.
Requests with no ``Origin`` at all (curl, same-origin GET navigations) are left
alone — an attacker web page cannot strip the header.

To allow non-default setups (e.g. binding to ``0.0.0.0`` and reaching the app via
a LAN IP or hostname), set ``WHISPER_TRUSTED_ORIGINS`` to a comma-separated list
of extra hosts or origins, e.g. ``WHISPER_TRUSTED_ORIGINS=192.168.1.50,my-box``.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

# Hosts that are always considered "the local app".
_DEFAULT_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _allowed_hosts() -> set[str]:
    """Trusted hostnames: the localhost set plus anything in
    ``WHISPER_TRUSTED_ORIGINS`` (read fresh each call so tests/env changes
    take effect without a restart)."""
    hosts = set(_DEFAULT_LOCAL_HOSTS)
    for item in os.environ.get("WHISPER_TRUSTED_ORIGINS", "").split(","):
        item = item.strip()
        if not item:
            continue
        # Accept either a bare host ("my-box") or a full origin
        # ("http://my-box:9000"); urlsplit handles both once we ensure a
        # leading "//" so it is treated as a netloc.
        parsed = urlsplit(item if "//" in item else f"//{item}")
        hosts.add(parsed.hostname or item)
    return hosts


def _host_of(value: str | None) -> str | None:
    """Extract the hostname from an Origin URL or a Host header value,
    correctly handling ports and IPv6 brackets."""
    if not value:
        return None
    parsed = urlsplit(value if "//" in value else f"//{value}")
    return parsed.hostname


def is_cross_site_origin(origin: str | None) -> bool:
    """True only if an ``Origin`` header is present AND points at an untrusted
    host. Missing/empty Origin is treated as same-site (not cross-site)."""
    if not origin:
        return False
    return _host_of(origin) not in _allowed_hosts()


def is_untrusted_host_header(host: str | None) -> bool:
    """True if a ``Host`` header is present AND not a trusted host. Blocks
    DNS-rebinding (the page keeps Host=attacker.com even after rebinding to
    127.0.0.1)."""
    if not host:
        return False
    return _host_of(host) not in _allowed_hosts()


async def origin_guard(request: Request, call_next):
    """ASGI HTTP middleware: reject cross-site Origin or untrusted Host."""
    if is_cross_site_origin(request.headers.get("origin")) or is_untrusted_host_header(
        request.headers.get("host")
    ):
        return JSONResponse(
            status_code=403,
            content={
                "error": "Cross-origin request rejected. Whisper Studio only "
                "accepts requests from the local app."
            },
        )
    return await call_next(request)


def is_ws_origin_allowed(origin: str | None) -> bool:
    """Origin check for WebSocket handshakes (the HTTP middleware does not see
    WS connections). A handshake with no Origin is allowed (native clients);
    a cross-site Origin is rejected to prevent cross-site WebSocket hijacking."""
    return not is_cross_site_origin(origin)
