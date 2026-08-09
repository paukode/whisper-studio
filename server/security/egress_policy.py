"""Outbound-network egress policy for sandboxed command execution.

DEFAULT-OFF FEATURE. Today, ``run_sandboxed`` (server/sandbox.py) opens a
network-unrestricted sandbox: the macOS/bwrap profile is ``(allow default)``
with no per-domain filtering, so any sandboxed command can reach any host on
the internet. ``_SANDBOX_ENV_DENYLIST`` in sandbox.py already stops one
specific leak (inherited GitHub tokens); this module addresses the general
case — restricting WHERE a sandboxed command can send bytes at all, for users
who opt in.

Three tiers, selected via ``permissions.json``'s ``network_policy.tier``:
  - "permissive"  — no restriction. Today's behavior. DEFAULT.
  - "curated"     — a seed allowlist of common package-registry/docs/API
                    domains (see CURATED_ALLOWLIST below), plus any
                    user-added custom domains.
  - "restrictive" — nothing is allowed outbound except domains the user has
                    explicitly added to ``custom_allowed_domains``.

Enforcement mechanism — an SNI-filtering local proxy
-----------------------------------------------------
Rather than terminating TLS (which would require minting and trusting a new
CA inside the sandboxed child — a much bigger footgun, and one more secret
that itself needs protecting), this module runs a small local TCP proxy that
never decrypts anything. For HTTPS traffic tunneled through it (the normal
case when a client honors ``HTTPS_PROXY`` and CONNECTs to us), the ONE
handshake message sent in cleartext by every TLS client, before any
encryption keys exist, is the ClientHello — and it carries the destination
hostname in its Server Name Indication (SNI) extension whenever the client
sets one (virtually all modern HTTP libraries and CLIs do, since it is
required for the server to pick the right TLS certificate on shared
hosting/CDNs). We parse just that one plaintext message, check the hostname
against the active tier's allowlist, and either relay the raw bytes
transparently to the real destination or refuse the connection. No key
material, no application data, no decryption — the proxy is exactly as
blind to the payload as a firewall would be, just smarter about the
destination.

Known limitations (read before trusting this as an airtight boundary)
----------------------------------------------------------------------
1. HTTPS_PROXY/HTTP_PROXY are environment-variable *conventions* that
   well-behaved HTTP libraries and CLIs (curl, pip, npm, git, requests, …)
   honor voluntarily. A command that opens a raw socket and never consults
   those variables (a bespoke `socket.connect` in arbitrary code, for
   instance) would ignore the proxy entirely. That is why server/sandbox.py
   ALSO adds `(deny network-outbound (remote ip "*:443"))` /
   `"*:80"` rules to the macOS profile when a non-permissive tier is active
   — forcing ALL direct outbound HTTP(S) through the kernel-level sandbox
   boundary, not just asking nicely via an env var. See sandbox.py for the
   Linux (bwrap) story, which is weaker (implemented but untested here).
2. Method-level restriction (``methods_only_safe``: block POST/PUT/PATCH/
   DELETE even to allowlisted domains) can only be enforced on the tiny
   sliver of traffic that arrives as plaintext HTTP (no CONNECT tunnel).
   The overwhelming majority of real traffic is HTTPS-over-CONNECT, where
   the HTTP method lives inside the encrypted TLS payload — we cannot see
   it without terminating TLS, which this design deliberately does not do.
   For that traffic, ``methods_only_safe`` has NO effect. This is
   documented, not silently pretended to work — see `_handle_connect`.
3. DNS (port 53) is not restricted or inspected. DNS-based exfiltration
   (encoding data in subdomain labels of otherwise-innocuous-looking
   queries) is a known technique this feature does not address. Out of
   scope for this change.
4. A ClientHello split across more than one TLS record (very large,
   rarely seen in practice — would need an unusually large session ticket
   or extension set) will fail SNI extraction; the CONNECT-tunnel path
   falls back to trusting the plaintext CONNECT target in that case (still
   allowlist-checked, just without the SNI cross-check — see
   `_handle_connect`).
"""

from __future__ import annotations

import logging
import selectors
import socket
import struct
import threading

log = logging.getLogger("whisper-studio")

# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------

TIER_PERMISSIVE = "permissive"
TIER_CURATED = "curated"
TIER_RESTRICTIVE = "restrictive"

VALID_TIERS = frozenset({TIER_PERMISSIVE, TIER_CURATED, TIER_RESTRICTIVE})

# The shape of `permissions.json`'s "network_policy" key. Kept as a plain
# literal (not imported by server/security/permissions.py) to avoid a
# circular import — permissions.py embeds an equivalent literal in its own
# DEFAULTS; keep the two in sync by hand if this shape changes.
DEFAULT_NETWORK_POLICY = {
    "tier": TIER_PERMISSIVE,
    "custom_allowed_domains": [],
    "methods_only_safe": False,
}

# ---------------------------------------------------------------------------
# Curated seed allowlist — common package-registry / docs / API domains a
# development sandbox legitimately needs to reach. Deliberately NOT
# exhaustive: grow it via a user's `custom_allowed_domains` for anything
# project-specific rather than expanding this list without bound. Grouped
# by ecosystem for reviewability. (70 domains.)
# ---------------------------------------------------------------------------
CURATED_ALLOWLIST: frozenset[str] = frozenset(
    {
        # npm / Node
        "npmjs.com",
        "www.npmjs.com",
        "npmjs.org",
        "registry.npmjs.org",
        "yarnpkg.com",
        "registry.yarnpkg.com",
        "nodejs.org",
        # Python / PyPI
        "pypi.org",
        "files.pythonhosted.org",
        "pythonhosted.org",
        "test.pypi.org",
        "python.org",
        "docs.python.org",
        # Ruby
        "rubygems.org",
        # GitHub (code hosting, raw content, release assets, API, git protocol)
        "github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
        "codeload.github.com",
        "api.github.com",
        "gist.githubusercontent.com",
        "githubusercontent.com",
        # GitLab / Bitbucket
        "gitlab.com",
        "bitbucket.org",
        # Docker / OCI container registries
        "docker.com",
        "www.docker.com",
        "registry-1.docker.io",
        "auth.docker.io",
        "production.cloudflare.docker.com",
        "ghcr.io",
        "quay.io",
        "gcr.io",
        # Rust
        "crates.io",
        "static.crates.io",
        "index.crates.io",
        "docs.rs",
        # Go
        "golang.org",
        "proxy.golang.org",
        "sum.golang.org",
        "pkg.go.dev",
        # .NET / NuGet
        "nuget.org",
        "www.nuget.org",
        "api.nuget.org",
        # Java / Maven
        "maven.org",
        "repo1.maven.org",
        "repo.maven.apache.org",
        "central.sonatype.com",
        # PHP / Composer
        "packagist.org",
        "repo.packagist.org",
        "getcomposer.org",
        # Perl / CPAN
        "metacpan.org",
        # R
        "cran.r-project.org",
        # Conda / Anaconda
        "anaconda.org",
        "conda.io",
        "repo.anaconda.com",
        "conda-forge.org",
        # Linux distro package mirrors
        "deb.debian.org",
        "archive.ubuntu.com",
        "security.ubuntu.com",
        # Homebrew
        "brew.sh",
        "formulae.brew.sh",
        # Hugging Face
        "huggingface.co",
        "cdn-lfs.huggingface.co",
        # Common CDNs used to fetch package assets
        "jsdelivr.net",
        "cdn.jsdelivr.net",
        "unpkg.com",
        "cdnjs.cloudflare.com",
        # Docs / Q&A references
        "readthedocs.io",
        "readthedocs.org",
        "developer.mozilla.org",
        "stackoverflow.com",
    }
)

# Restrictive tier: nothing, by design. Only custom_allowed_domains apply.
RESTRICTIVE_ALLOWLIST: frozenset[str] = frozenset()


def tier_allowlist(tier: str) -> frozenset[str]:
    """The seed allowlist for a tier, before adding the user's custom domains.
    Permissive has no allowlist concept (nothing is checked), so it returns
    empty here — callers must special-case permissive via is_domain_allowed."""
    if tier == TIER_CURATED:
        return CURATED_ALLOWLIST
    if tier == TIER_RESTRICTIVE:
        return RESTRICTIVE_ALLOWLIST
    return frozenset()


def _normalize_domain(domain: str) -> str:
    return domain.strip().lower().rstrip(".")


def is_domain_allowed(hostname: str, policy: dict) -> bool:
    """Whether `hostname` may be reached under the given network_policy dict
    (the same shape as DEFAULT_NETWORK_POLICY / permissions.json's
    "network_policy" key).

    A hostname matches an allowlist entry either exactly or as a subdomain
    (e.g. "objects.githubusercontent.com" matches an entry of
    "githubusercontent.com"), so users don't need to enumerate every
    possible subdomain a registry happens to use.
    """
    tier = policy.get("tier", TIER_PERMISSIVE)
    if tier == TIER_PERMISSIVE:
        return True
    hostname = _normalize_domain(hostname)
    if not hostname:
        return False
    custom = policy.get("custom_allowed_domains") or []
    allowed = tier_allowlist(tier) | {_normalize_domain(d) for d in custom if d and d.strip()}
    for domain in allowed:
        if hostname == domain or hostname.endswith("." + domain):
            return True
    return False


def get_active_policy() -> dict:
    """The live network_policy config from permissions.json, merged over
    DEFAULT_NETWORK_POLICY so a partially-saved/older config still has every
    key. Cheap file read (same cost as permissions.load_permissions()
    elsewhere) — call fresh each time so a tier change in settings takes
    effect on the very next sandboxed command, no restart needed."""
    from server.security.permissions import load_permissions

    saved = load_permissions().get("network_policy") or {}
    return {**DEFAULT_NETWORK_POLICY, **saved}


# ---------------------------------------------------------------------------
# TLS ClientHello / SNI parsing
#
# No decryption happens anywhere in this module. The ClientHello is the
# first message of a TLS handshake and — by protocol design, so servers can
# pick a certificate before any key exchange happens — is sent in the clear.
# Everything after it (the rest of the handshake, and all application data)
# stays fully encrypted and is never inspected; we only ever look at this
# one plaintext framing message.
# ---------------------------------------------------------------------------

_TLS_HANDSHAKE_CONTENT_TYPE = 0x16
_TLS_CLIENT_HELLO_MSG_TYPE = 0x01
_TLS_RECORD_HEADER_LEN = 5
_TLS_EXTENSION_SERVER_NAME = 0x0000
_SNI_NAME_TYPE_HOST_NAME = 0x00


def extract_sni(data: bytes) -> str | None:
    """Parse the SNI hostname out of a single TLS record containing a
    ClientHello. Returns None if `data` isn't a well-formed, COMPLETE
    handshake record with a host_name SNI entry — including the case where
    `data` is simply too short (an incomplete read); callers reading live
    from a socket should keep accumulating bytes and retry rather than
    treat a None here as a hard failure until they've hit a size cap (see
    `_recv_full_clienthello` in the proxy below).

    Deliberately conservative: any malformed-looking input just returns
    None (fail closed on ambiguity — callers fall back to whatever
    non-SNI-based check they have, e.g. the plaintext CONNECT target).
    """
    try:
        if len(data) < _TLS_RECORD_HEADER_LEN:
            return None
        if data[0] != _TLS_HANDSHAKE_CONTENT_TYPE:
            return None
        record_len = struct.unpack(">H", data[3:5])[0]
        end = _TLS_RECORD_HEADER_LEN + record_len
        if len(data) < end:
            return None  # incomplete — caller should read more and retry
        handshake = data[_TLS_RECORD_HEADER_LEN:end]

        if len(handshake) < 4 or handshake[0] != _TLS_CLIENT_HELLO_MSG_TYPE:
            return None
        hs_len = int.from_bytes(handshake[1:4], "big")
        body = handshake[4 : 4 + hs_len]

        pos = 2 + 32  # client_version(2) + random(32)
        if pos >= len(body):
            return None

        session_id_len = body[pos]
        pos += 1 + session_id_len
        if pos + 2 > len(body):
            return None

        cipher_suites_len = struct.unpack(">H", body[pos : pos + 2])[0]
        pos += 2 + cipher_suites_len
        if pos >= len(body):
            return None

        compression_len = body[pos]
        pos += 1 + compression_len
        if pos + 2 > len(body):
            return None  # no extensions block present at all

        extensions_len = struct.unpack(">H", body[pos : pos + 2])[0]
        pos += 2
        extensions_end = min(pos + extensions_len, len(body))

        while pos + 4 <= extensions_end:
            ext_type = struct.unpack(">H", body[pos : pos + 2])[0]
            ext_len = struct.unpack(">H", body[pos + 2 : pos + 4])[0]
            ext_data = body[pos + 4 : pos + 4 + ext_len]
            if ext_type == _TLS_EXTENSION_SERVER_NAME:
                return _parse_server_name_extension(ext_data)
            pos += 4 + ext_len
        return None
    except (struct.error, IndexError, UnicodeDecodeError):
        return None


def _parse_server_name_extension(ext_data: bytes) -> str | None:
    if len(ext_data) < 2:
        return None
    list_len = struct.unpack(">H", ext_data[0:2])[0]
    entries = ext_data[2 : 2 + list_len]
    pos = 0
    while pos + 3 <= len(entries):
        name_type = entries[pos]
        name_len = struct.unpack(">H", entries[pos + 1 : pos + 3])[0]
        name = entries[pos + 3 : pos + 3 + name_len]
        if name_type == _SNI_NAME_TYPE_HOST_NAME:
            return name.decode("ascii")
        pos += 3 + name_len
    return None


# ---------------------------------------------------------------------------
# The local SNI-filtering proxy
# ---------------------------------------------------------------------------

# Generous upper bound on how much of a CONNECT request / ClientHello we'll
# buffer before giving up — real requests are tiny fractions of this; it
# only exists to bound memory/time on a hostile or broken client.
_MAX_HEADER_BYTES = 8192
_MAX_CLIENTHELLO_BYTES = 16384  # a TLS record's own length field caps at 16KB
_HANDSHAKE_TIMEOUT_S = 10
_IDLE_RELAY_TIMEOUT_S = 300  # close a tunnel that's gone quiet for 5 minutes

_CONNECT_METHOD = b"CONNECT"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _recv_until(sock: socket.socket, marker: bytes, max_bytes: int) -> tuple[bytes, bytes]:
    """Read from `sock` until `marker` has been seen, returning
    `(head, leftover)` where `head` ends exactly at (and includes) the
    marker and `leftover` is any bytes read PAST it.

    That split matters: a single `recv()` call can return more than one
    logical message's worth of bytes (e.g. a client that pipelines its
    CONNECT request and its ClientHello back-to-back, with no delay, is
    common — some HTTP libraries don't wait for the 200 before starting the
    TLS handshake). Early versions of this function returned the whole
    accumulated buffer and callers only ever looked at the first line,
    silently discarding `leftover` — which then meant the "peek the
    ClientHello" step in `_handle_connect` blocked on a fresh `recv()` for
    bytes that had, in fact, already arrived and been read (just dropped),
    hanging until its own timeout fired. Caught by
    tests/test_egress_policy.py flaking intermittently; fixed by threading
    `leftover` through to whichever handler needs it next.

    Raises ValueError/ConnectionError on abuse or hangup — callers treat
    both as "can't service this connection, close it".
    """
    buf = b""
    while marker not in buf:
        if len(buf) > max_bytes:
            raise ValueError("request head exceeded size limit")
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("peer closed before sending a complete request")
        buf += chunk
    idx = buf.index(marker) + len(marker)
    return buf[:idx], buf[idx:]


def _recv_full_clienthello(sock: socket.socket, already: bytes) -> bytes:
    """Keep reading until `already` contains a complete TLS record (per its
    own length field) or we hit `_MAX_CLIENTHELLO_BYTES`. Returns whatever
    was accumulated either way — extract_sni() itself decides if it's
    parseable; this just avoids handing extract_sni a chopped-off record
    when a few more bytes were still in flight."""
    buf = already
    while True:
        if len(buf) >= _TLS_RECORD_HEADER_LEN:
            record_len = struct.unpack(">H", buf[3:5])[0]
            if len(buf) >= _TLS_RECORD_HEADER_LEN + record_len:
                return buf
        if len(buf) > _MAX_CLIENTHELLO_BYTES:
            return buf
        try:
            chunk = sock.recv(4096)
        except OSError:
            return buf
        if not chunk:
            return buf
        buf += chunk


def _relay(
    client_sock: socket.socket, upstream_sock: socket.socket, preloaded: bytes = b""
) -> None:
    """Bidirectional byte-for-byte relay between two already-connected
    sockets, until either side closes or goes idle past
    `_IDLE_RELAY_TIMEOUT_S`. `preloaded` is bytes already read from the
    client (e.g. the first chunk of the ClientHello, consumed while
    sniffing SNI) that must be forwarded to upstream before relaying
    continues — otherwise those bytes would simply be lost.

    This is the ONLY place payload bytes are touched, and they are never
    inspected here — just moved. Runs in the calling thread using a
    selector so a single thread services both directions of one
    connection (keeps thread count to one per connection, not two)."""
    if preloaded:
        try:
            upstream_sock.sendall(preloaded)
        except OSError:
            return

    sel = selectors.DefaultSelector()
    sel.register(client_sock, selectors.EVENT_READ, data="client")
    sel.register(upstream_sock, selectors.EVENT_READ, data="upstream")
    try:
        while True:
            events = sel.select(timeout=_IDLE_RELAY_TIMEOUT_S)
            if not events:
                return  # idle timeout
            for key, _ in events:
                src = key.fileobj
                dst = upstream_sock if key.data == "client" else client_sock
                try:
                    data = src.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    dst.sendall(data)
                except OSError:
                    return
    finally:
        sel.close()


def handle_connection(client_sock: socket.socket) -> None:
    """Service one incoming proxy connection end to end. Two shapes of
    request are supported, matching what HTTPS_PROXY/HTTP_PROXY-aware
    clients actually send:

    1. `CONNECT host:port HTTP/1.1` — the standard way curl/pip/npm/git/etc
       tunnel HTTPS through a configured proxy. We reply 200, then the
       client's real TLS handshake rides on top of this same socket. See
       `_handle_connect`.
    2. A plain absolute-form HTTP request (`GET http://host/path HTTP/1.1`)
       — used for plain (non-TLS) `http://` URLs under HTTP_PROXY. See
       `_handle_plain_http`. This is also the ONLY path where
       `methods_only_safe` can be enforced — see module docstring point 2.

    Anything else (garbage, a client that doesn't speak proxy protocol at
    all) is closed without a response.
    """
    client_sock.settimeout(_HANDSHAKE_TIMEOUT_S)
    try:
        head, leftover = _recv_until(client_sock, b"\r\n\r\n", _MAX_HEADER_BYTES)
    except (TimeoutError, ValueError, ConnectionError, OSError):
        return

    request_line = head.split(b"\r\n", 1)[0]
    policy = get_active_policy()

    if request_line.startswith(_CONNECT_METHOD + b" "):
        parsed = _parse_connect_line(request_line)
        if parsed is None:
            return
        host, port = parsed
        _handle_connect(client_sock, host, port, policy, leftover)
        return

    _handle_plain_http(client_sock, head, policy, leftover)


def _parse_connect_line(request_line: bytes) -> tuple[str, int] | None:
    # "CONNECT host:port HTTP/1.1"
    parts = request_line.split(b" ")
    if len(parts) < 2:
        return None
    target = parts[1]
    if b":" not in target:
        return None
    host_b, _, port_b = target.rpartition(b":")
    try:
        port = int(port_b)
    except ValueError:
        return None
    host = host_b.decode("ascii", "ignore")
    if not host:
        return None
    return host, port


def _handle_connect(
    client_sock: socket.socket, host: str, port: int, policy: dict, leftover: bytes = b""
) -> None:
    if not is_domain_allowed(host, policy):
        log.warning("egress proxy: blocked CONNECT %s:%s (tier=%s)", host, port, policy.get("tier"))
        _send_error(client_sock, b"403 Forbidden")
        return

    try:
        client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    except OSError:
        return

    # Defense-in-depth: sniff the ClientHello the client sends over the now
    # -established tunnel and cross-check its SNI against the CONNECT
    # target we just allowlist-checked above. A client that CONNECTs to an
    # allowed hostname but then negotiates TLS toward a *different* one is
    # trying to smuggle traffic past a proxy that only reads the CONNECT
    # line — refuse that outright, even though the CONNECT target alone
    # already passed. If the tunneled bytes don't parse as a TLS
    # ClientHello at all (not everything over a CONNECT tunnel has to be
    # TLS), we fall back to trusting the CONNECT-target check alone.
    #
    # `leftover` may already hold the start of this (a client that pipelines
    # its ClientHello right after CONNECT, without waiting for the 200, can
    # have it arrive in the same read as the CONNECT headers) — only issue a
    # fresh recv() if nothing came along for free.
    first_bytes = leftover
    if not first_bytes:
        try:
            first_bytes = client_sock.recv(4096)
        except OSError:
            first_bytes = b""

    preloaded = first_bytes
    if first_bytes[:1] == bytes([_TLS_HANDSHAKE_CONTENT_TYPE]):
        preloaded = _recv_full_clienthello(client_sock, first_bytes)
        sni = extract_sni(preloaded)
        if sni is not None and _normalize_domain(sni) != _normalize_domain(host):
            log.warning(
                "egress proxy: SNI %r does not match CONNECT target %r — closing (possible bypass attempt)",
                sni,
                host,
            )
            return

    try:
        upstream = socket.create_connection((host, port), timeout=_HANDSHAKE_TIMEOUT_S)
    except OSError as e:
        log.warning("egress proxy: could not reach allowlisted %s:%s (%s)", host, port, e)
        return
    try:
        client_sock.settimeout(None)
        upstream.settimeout(None)
        _relay(client_sock, upstream, preloaded=preloaded)
    finally:
        try:
            upstream.close()
        except OSError:
            pass


def _handle_plain_http(
    client_sock: socket.socket, head: bytes, policy: dict, leftover: bytes = b""
) -> None:
    """Plain (non-TLS) HTTP forwarding. Used when a client sends an
    absolute-form request directly (typical only for explicit `http://`
    URLs under HTTP_PROXY) rather than tunneling via CONNECT. Because these
    bytes are cleartext, this is the one place `methods_only_safe` can
    actually be enforced end to end — see module docstring point 2 for why
    it can't be enforced on the (far more common) CONNECT/HTTPS path.

    `leftover` is any request-body bytes that arrived in the same read as
    the header terminator (see `_recv_until`) — forwarded on ahead of
    whatever the relay picks up next so nothing sent before we finished
    reading headers gets silently dropped."""
    request_line = head.split(b"\r\n", 1)[0]
    parts = request_line.split(b" ")
    if len(parts) < 2:
        return
    method = parts[0].decode("ascii", "ignore").upper()
    target = parts[1].decode("ascii", "ignore")

    host = _extract_host(target, head)
    if not host:
        return

    if policy.get("methods_only_safe") and method not in _SAFE_METHODS:
        log.warning("egress proxy: blocked %s %s (methods_only_safe)", method, host)
        _send_error(client_sock, b"403 Forbidden")
        return

    if not is_domain_allowed(host, policy):
        log.warning("egress proxy: blocked %s %s (tier=%s)", method, host, policy.get("tier"))
        _send_error(client_sock, b"403 Forbidden")
        return

    try:
        upstream = socket.create_connection((host, 80), timeout=_HANDSHAKE_TIMEOUT_S)
    except OSError as e:
        log.warning("egress proxy: could not reach %s:80 (%s)", host, e)
        return
    try:
        client_sock.settimeout(None)
        upstream.settimeout(None)
        _relay(client_sock, upstream, preloaded=head + leftover)
    finally:
        try:
            upstream.close()
        except OSError:
            pass


def _extract_host(target: str, head: bytes) -> str | None:
    if target.startswith("http://"):
        rest = target[len("http://") :]
        host = rest.split("/", 1)[0]
        return host.split(":")[0] or None
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(b"host:"):
            hostport = line.split(b":", 1)[1].strip().decode("ascii", "ignore")
            return hostport.split(":")[0] or None
    return None


def _send_error(client_sock: socket.socket, status: bytes) -> None:
    try:
        client_sock.sendall(b"HTTP/1.1 " + status + b"\r\n\r\n")
    except OSError:
        pass


class EgressProxy:
    """A threaded TCP listener bound to loopback that runs `handle_connection`
    for every accepted socket. One instance is meant to live for the whole
    server process (see `ensure_proxy_running`), started lazily the first
    time a non-permissive policy is actually used."""

    def __init__(self, host: str = "127.0.0.1"):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, 0))  # port 0 -> OS-assigned ephemeral port
        self._sock.listen(128)
        self.address: tuple[str, int] = self._sock.getsockname()[:2]
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._serve_forever, daemon=True, name="egress-proxy"
        )
        self._thread.start()

    def _serve_forever(self) -> None:
        while True:
            try:
                client_sock, _addr = self._sock.accept()
            except OSError:
                return  # listener socket closed
            threading.Thread(target=self._handle_safely, args=(client_sock,), daemon=True).start()

    def _handle_safely(self, client_sock: socket.socket) -> None:
        try:
            handle_connection(client_sock)
        except Exception:
            log.exception("egress proxy: unhandled error servicing a connection")
        finally:
            try:
                client_sock.close()
            except OSError:
                pass

    def stop(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


_proxy_lock = threading.Lock()
_proxy_instance: EgressProxy | None = None


def ensure_proxy_running() -> tuple[str, int]:
    """Idempotent: starts the singleton proxy on first call, returns the
    already-running instance's (host, port) on every subsequent call. The
    proxy itself re-reads the active policy on every connection (see
    `handle_connection` -> `get_active_policy`), so it does not need to be
    restarted when the tier changes — only started once, lazily, the first
    time it's actually needed."""
    global _proxy_instance
    with _proxy_lock:
        if _proxy_instance is None:
            _proxy_instance = EgressProxy()
            _proxy_instance.start()
        return _proxy_instance.address


def _reset_proxy_for_tests() -> None:
    """Test-only: stop and forget the singleton so a test can start a fresh
    instance (e.g. to assert on a freshly bound port). Not used by
    production code."""
    global _proxy_instance
    with _proxy_lock:
        if _proxy_instance is not None:
            _proxy_instance.stop()
        _proxy_instance = None
