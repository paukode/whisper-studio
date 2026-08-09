"""Tests for server/security/egress_policy.py: the tier/allowlist semantics,
SNI ClientHello parsing, and the SNI-filtering proxy's connection handling.

No test here makes a real internet connection or a real TLS handshake — the
ClientHello test constructs a minimal synthetic one, and the proxy
integration test uses a local fake "origin" TCP server on loopback.
"""

import socket
import struct
import threading

from server.security import egress_policy as ep

# ── tier / allowlist semantics ──────────────────────────────────────────────


def test_permissive_allows_anything():
    policy = {"tier": "permissive", "custom_allowed_domains": [], "methods_only_safe": False}
    assert ep.is_domain_allowed("evil.example", policy) is True
    assert ep.is_domain_allowed("", policy) is True


def test_curated_allows_seed_domains_exactly_and_as_subdomain():
    policy = {"tier": "curated", "custom_allowed_domains": [], "methods_only_safe": False}
    assert ep.is_domain_allowed("pypi.org", policy) is True
    assert ep.is_domain_allowed("registry.npmjs.org", policy) is True
    # subdomain of an allowlisted entry
    assert ep.is_domain_allowed("mirror.registry.npmjs.org", policy) is True
    # case/trailing-dot insensitivity
    assert ep.is_domain_allowed("PyPI.org.", policy) is True


def test_curated_denies_arbitrary_domain():
    policy = {"tier": "curated", "custom_allowed_domains": [], "methods_only_safe": False}
    assert ep.is_domain_allowed("evil.example", policy) is False
    # a lookalike must not match via naive substring/prefix checks
    assert ep.is_domain_allowed("notpypi.org", policy) is False
    assert ep.is_domain_allowed("pypi.org.evil.example", policy) is False


def test_restrictive_denies_everything_except_custom():
    policy = {
        "tier": "restrictive",
        "custom_allowed_domains": ["internal.example.org"],
        "methods_only_safe": False,
    }
    assert ep.is_domain_allowed("pypi.org", policy) is False  # curated seed doesn't apply
    assert ep.is_domain_allowed("internal.example.org", policy) is True
    assert ep.is_domain_allowed("host.internal.example.org", policy) is True
    assert ep.is_domain_allowed("other.example", policy) is False


def test_curated_allowlist_seed_size_is_in_the_requested_range():
    # The task asked for a seed allowlist of roughly 50-70 domains.
    assert 50 <= len(ep.CURATED_ALLOWLIST) <= 70


def test_get_active_policy_merges_partial_saved_config(monkeypatch):
    # A config saved before methods_only_safe existed, or missing a key some
    # other way, should still come back with every key populated.
    monkeypatch.setattr(
        "server.security.permissions.load_permissions",
        lambda: {"network_policy": {"tier": "curated"}},
    )
    policy = ep.get_active_policy()
    assert policy["tier"] == "curated"
    assert policy["custom_allowed_domains"] == []
    assert policy["methods_only_safe"] is False


# ── TLS ClientHello / SNI parsing ────────────────────────────────────────────


def _build_client_hello(hostname: bytes) -> bytes:
    """A minimal, syntactically valid TLS 1.2 ClientHello record carrying a
    single SNI (server_name) extension for `hostname`. Not a real captured
    packet — built field-by-field per RFC 5246/6066 so the test documents
    exactly what shape extract_sni() is expected to parse."""
    server_name_entry = struct.pack(">B", 0) + struct.pack(">H", len(hostname)) + hostname
    server_name_list = struct.pack(">H", len(server_name_entry)) + server_name_entry
    sni_extension = struct.pack(">HH", 0x0000, len(server_name_list)) + server_name_list

    extensions = sni_extension
    session_id = b""
    cipher_suites = bytes([0x00, 0x2F])  # TLS_RSA_WITH_AES_128_CBC_SHA
    compression_methods = bytes([0x00])  # null

    body = struct.pack(">H", 0x0303)  # client_version = TLS 1.2
    body += bytes(32)  # random
    body += struct.pack(">B", len(session_id)) + session_id
    body += struct.pack(">H", len(cipher_suites)) + cipher_suites
    body += struct.pack(">B", len(compression_methods)) + compression_methods
    body += struct.pack(">H", len(extensions)) + extensions

    handshake = struct.pack(">B", 0x01) + len(body).to_bytes(3, "big") + body  # ClientHello
    record = struct.pack(">B", 0x16) + struct.pack(">H", 0x0301) + struct.pack(">H", len(handshake))
    return record + handshake


def test_extract_sni_from_synthetic_clienthello():
    data = _build_client_hello(b"example.com")
    assert ep.extract_sni(data) == "example.com"


def test_extract_sni_from_synthetic_clienthello_longer_hostname():
    data = _build_client_hello(b"registry.npmjs.org")
    assert ep.extract_sni(data) == "registry.npmjs.org"


def test_extract_sni_returns_none_for_non_tls_bytes():
    assert ep.extract_sni(b"GET / HTTP/1.1\r\n\r\n") is None


def test_extract_sni_returns_none_for_incomplete_record():
    data = _build_client_hello(b"example.com")
    assert ep.extract_sni(data[:10]) is None  # truncated mid-handshake


def test_extract_sni_returns_none_for_empty_bytes():
    assert ep.extract_sni(b"") is None


# ── the SNI-filtering proxy: connection handling ────────────────────────────


class _FakeOrigin:
    """A trivial loopback TCP "origin server" standing in for the real
    internet destination: waits for the client to speak first (as any real
    protocol tunneled through CONNECT — TLS, HTTP — requires), then echoes
    back a fixed banner. Speaking only after the client does mirrors how the
    proxy's own peek-then-relay logic expects tunneled traffic to behave."""

    def __init__(self, banner: bytes = b"hello from origin"):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._banner = banner
        self._thread = threading.Thread(target=self._serve_one, daemon=True)
        self._thread.start()

    def _serve_one(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        try:
            # Generous timeout: this is a loopback-only test helper, but the
            # full test suite runs thousands of tests (some spawning real
            # subprocesses) that can starve the GIL/scheduler briefly, so a
            # tight timeout here has caused flakiness under full-suite load.
            conn.settimeout(15)
            conn.recv(4096)  # wait for the client's first bytes, then respond
            conn.sendall(self._banner)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


def _client_connect_and_read(
    port: int, connect_request: bytes, post_connect_payload: bytes = b"", read_timeout=15
) -> bytes:
    """Send `connect_request`, then (if given) `post_connect_payload` — the
    "ClientHello" a real proxy-aware TLS client would send immediately after
    a successful CONNECT — and read back whatever the proxy relays."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=read_timeout)
    sock.settimeout(read_timeout)
    sock.sendall(connect_request)
    if post_connect_payload:
        sock.sendall(post_connect_payload)
    chunks = []
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    except TimeoutError:
        pass
    sock.close()
    return b"".join(chunks)


def test_proxy_relays_connect_tunnel_for_allowed_host(monkeypatch):
    monkeypatch.setattr(
        ep,
        "get_active_policy",
        lambda: {
            "tier": "restrictive",
            "custom_allowed_domains": ["127.0.0.1"],
            "methods_only_safe": False,
        },
    )
    origin = _FakeOrigin(banner=b"payload-from-allowed-origin")
    try:
        proxy_host, proxy_port = _start_test_proxy()
        request = f"CONNECT 127.0.0.1:{origin.port} HTTP/1.1\r\nHost: 127.0.0.1:{origin.port}\r\n\r\n".encode()
        response = _client_connect_and_read(proxy_port, request, post_connect_payload=b"ping")
        assert b"200 Connection Established" in response
        assert b"payload-from-allowed-origin" in response
    finally:
        origin.close()


def test_proxy_relays_pipelined_payload_sent_in_the_same_packet_as_connect(monkeypatch):
    """Regression test: a client that doesn't wait for the 200 response
    before starting to send tunnel bytes — sending CONNECT headers and its
    first payload bytes in one `sendall()`/TCP segment — used to have that
    payload silently dropped. `_recv_until` only ever returned callers the
    first line of what it read; if the single recv() that satisfied the
    "\\r\\n\\r\\n" search happened to also contain bytes belonging to the
    NEXT message, those extra bytes were discarded rather than handed back
    as `leftover`, and `_handle_connect`'s subsequent `client_sock.recv()`
    for the ClientHello then blocked waiting for bytes that had already
    arrived and been thrown away — hanging until its own timeout, so the
    tunnel never carried the payload through. This sends both in a single
    `sendall` call to force exactly that single-read interleaving
    deterministically (rather than relying on scheduler timing)."""
    monkeypatch.setattr(
        ep,
        "get_active_policy",
        lambda: {
            "tier": "restrictive",
            "custom_allowed_domains": ["127.0.0.1"],
            "methods_only_safe": False,
        },
    )
    origin = _FakeOrigin(banner=b"payload-from-allowed-origin")
    try:
        proxy_host, proxy_port = _start_test_proxy()
        request = f"CONNECT 127.0.0.1:{origin.port} HTTP/1.1\r\nHost: 127.0.0.1:{origin.port}\r\n\r\n".encode()
        sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=15)
        sock.settimeout(15)
        sock.sendall(request + b"ping")  # ONE send call: headers + payload together
        chunks = []
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except TimeoutError:
            pass
        sock.close()
        response = b"".join(chunks)
        assert b"200 Connection Established" in response
        assert b"payload-from-allowed-origin" in response
    finally:
        origin.close()


def test_proxy_refuses_connect_tunnel_for_disallowed_host(monkeypatch):
    monkeypatch.setattr(
        ep,
        "get_active_policy",
        lambda: {"tier": "restrictive", "custom_allowed_domains": [], "methods_only_safe": False},
    )
    origin = _FakeOrigin(banner=b"should-never-be-seen")
    try:
        proxy_host, proxy_port = _start_test_proxy()
        request = f"CONNECT 127.0.0.1:{origin.port} HTTP/1.1\r\nHost: 127.0.0.1:{origin.port}\r\n\r\n".encode()
        response = _client_connect_and_read(proxy_port, request)
        assert b"403" in response
        assert b"should-never-be-seen" not in response
    finally:
        origin.close()


def test_proxy_plain_http_enforces_methods_only_safe(monkeypatch):
    monkeypatch.setattr(
        ep,
        "get_active_policy",
        lambda: {
            "tier": "restrictive",
            "custom_allowed_domains": ["127.0.0.1"],
            "methods_only_safe": True,
        },
    )
    proxy_host, proxy_port = _start_test_proxy()
    request = b"POST http://127.0.0.1/whatever HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
    response = _client_connect_and_read(proxy_port, request)
    assert b"403" in response


def _start_test_proxy():
    """Start a fresh, isolated proxy instance for a single test (not the
    production singleton, so tests can't interfere with each other)."""
    proxy = ep.EgressProxy()
    proxy.start()
    return proxy.address
