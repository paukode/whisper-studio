"""web_fetch is model-controlled, so it must refuse to fetch internal/private
addresses (SSRF) — localhost, RFC1918, and the cloud-metadata IP — and reject
non-http(s) schemes."""

import email.message

from server.executors.web import (
    _BlockedRedirect,
    _is_safe_public_url,
    _SSRFSafeRedirectHandler,
    exec_web_fetch,
)


def test_blocks_internal_addresses():
    for url in [
        "http://127.0.0.1/",
        "http://localhost:8000/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://192.168.1.1/",
        "http://10.0.0.5/admin",
        "http://[::1]/",
    ]:
        ok, _ = _is_safe_public_url(url)
        assert not ok, f"{url} should be blocked"


def test_blocks_non_http_schemes():
    assert not _is_safe_public_url("file:///etc/passwd")[0]
    assert not _is_safe_public_url("ftp://example.com/")[0]


def test_allows_public_address():
    ok, reason = _is_safe_public_url("http://1.1.1.1/")
    assert ok, reason


def test_exec_web_fetch_rejects_internal_url():
    out = exec_web_fetch({"url": "http://169.254.169.254/latest/meta-data/"}, [], [])
    assert out.startswith("[error]")
    assert "private" in out or "internal" in out


def _fake_redirect(newurl):
    """Drive the redirect handler exactly as urllib would on a 302, using a
    hermetic (no-network) request/headers pair. Returns the handler's result or
    lets it raise."""
    import urllib.request

    handler = _SSRFSafeRedirectHandler()
    req = urllib.request.Request("http://example.com/")
    headers = email.message.Message()
    return handler.redirect_request(req, None, 302, "Found", headers, newurl)


def test_redirect_handler_blocks_loopback():
    # A public URL that redirects to loopback must be blocked per-hop.
    import pytest

    with pytest.raises(_BlockedRedirect):
        _fake_redirect("http://127.0.0.1/")


def test_redirect_handler_blocks_metadata():
    # The cloud-metadata address is the classic SSRF-via-redirect target.
    import pytest

    with pytest.raises(_BlockedRedirect):
        _fake_redirect("http://169.254.169.254/latest/meta-data/")


def test_redirect_handler_allows_safe_public_target():
    # A redirect to another safe public address is still followed (returns a
    # new Request pointing at the target), so normal redirects keep working.
    new = _fake_redirect("http://1.1.1.1/")
    assert new is not None
    assert new.full_url == "http://1.1.1.1/"
