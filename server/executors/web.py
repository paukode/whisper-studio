import logging
import urllib.request
from html.parser import HTMLParser

from server.executors import register_executor
from server.infrastructure.config import get as config_get

log = logging.getLogger("whisper-studio")


class _HTMLToText(HTMLParser):
    """Converts fetched HTML into readable markdown-ish text: headings become
    #-prefixed lines, list items get bullets, and <a href> becomes
    [text](href). Anchor-only and javascript: links are left as plain text."""

    def __init__(self):
        super().__init__()
        self._parts = []
        self._skip = False
        self._in_pre = False
        self._block_tags = {
            "p",
            "div",
            "br",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "li",
            "tr",
            "blockquote",
            "section",
            "article",
            "header",
            "footer",
        }
        self._heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
        self._skip_tags = {"script", "style", "noscript", "svg", "path"}
        self._current_heading = None
        self._link_hrefs = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self._skip_tags:
            self._skip = True
        if tag == "pre" or tag == "code":
            self._in_pre = True
        if tag in self._heading_tags:
            level = int(tag[1])
            self._current_heading = "#" * level + " "
            self._parts.append("\n\n")
        elif tag in self._block_tags:
            self._parts.append("\n")
        if tag == "li":
            self._parts.append("- ")
        if tag == "a":
            href = next((val for name, val in attrs if name == "href"), None)
            if href and not href.startswith(("#", "javascript:")):
                self._parts.append("[")
                self._link_hrefs.append(href)
        if tag == "img":
            alt = ""
            for name, val in attrs:
                if name == "alt" and val:
                    alt = val
            if alt:
                self._parts.append(f"[Image: {alt}]")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self._skip_tags:
            self._skip = False
        if tag == "pre" or tag == "code":
            self._in_pre = False
        if tag in self._heading_tags:
            self._current_heading = None
            self._parts.append("\n")
        if tag in self._block_tags:
            self._parts.append("\n")
        if tag == "a" and self._link_hrefs:
            self._parts.append(f"]({self._link_hrefs.pop()})")

    def handle_data(self, data):
        if self._skip:
            return
        if self._current_heading:
            self._parts.append(self._current_heading)
            self._current_heading = None
        if not self._in_pre:
            data = " ".join(data.split())
        if data:
            self._parts.append(data)

    def get_text(self):
        import re

        # Pages that never close an <a> would otherwise leave a dangling "[".
        while self._link_hrefs:
            self._parts.append(f"]({self._link_hrefs.pop()})")
        text = "".join(self._parts)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _is_safe_public_url(url: str) -> tuple[bool, str]:
    """SSRF guard for web_fetch. The URL is model-controlled, so before
    fetching we require an http(s) scheme and confirm the host resolves only to
    public (global) IP addresses — blocking localhost, private RFC1918 ranges,
    link-local (incl. the cloud-metadata address 169.254.169.254), and other
    reserved space. Resolving here also defeats DNS names that point at
    internal IPs. Returns (ok, reason)."""
    import ipaddress
    import socket
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False, f"blocked URL scheme: {parts.scheme or '(none)'}"
    host = parts.hostname
    if not host:
        return False, "URL has no host"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False, "could not resolve host"
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False, "host resolved to an invalid address"
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            return False, "refusing to fetch a private or internal address"
    return True, ""


class _BlockedRedirect(Exception):
    """Raised when a 30x redirect points at an address the SSRF guard rejects.
    Carries the target url and the guard's reason so the caller can surface a
    clear error instead of silently following the redirect."""

    def __init__(self, reason: str, url: str):
        super().__init__(reason)
        self.reason = reason
        self.url = url


class _SSRFSafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """urllib follows HTTP 30x redirects automatically, and the initial
    ``_is_safe_public_url`` check only validates the first URL. Without per-hop
    re-validation a public URL could redirect to an internal address (loopback,
    RFC1918, or the cloud-metadata IP) and defeat the SSRF guard. This handler
    re-runs the guard on every redirect target and blocks unsafe hops. The
    inherited ``max_redirections`` cap (10) still applies."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        ok, reason = _is_safe_public_url(newurl)
        if not ok:
            raise _BlockedRedirect(reason, newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _agentcore_browser_enabled() -> bool:
    """True if an Amazon Bedrock AgentCore MCP server (browser tools) is enabled,
    so web search can fall back to browsing on the user's AWS account."""
    try:
        from server.mcp import mcp_manager

        servers = mcp_manager.load_config().get("servers", {})
        for conf in servers.values():
            if conf.get("enabled") and "agentcore" in " ".join(conf.get("args", [])).lower():
                return True
    except Exception:  # noqa: BLE001 — never let provider detection break a search
        pass
    return False


@register_executor("web_search", read_only=True, concurrent_safe=True)
def exec_web_search(tool_input, transcript, current_attachments):
    query = tool_input["query"]
    api_key = config_get("tavily_api_key", "")
    # Tier 1: Tavily, if a key is configured (fast, one-shot results).
    if api_key:
        try:
            from tavily import TavilyClient

            client = TavilyClient(api_key=api_key)
            log.info("Tavily search: %s", query)
            results = client.search(query, max_results=5)
            log.info("Tavily results: %d", len(results.get("results", [])))
            parts = []
            for r in results.get("results", []):
                parts.append(f"Title: {r['title']}\nURL: {r['url']}\n{r['content']}")
            return "\n\n".join(parts) if parts else "No results found."
        except ImportError:
            log.error("tavily-python not installed")
            return "Web search unavailable: tavily-python package not installed."
        except Exception as e:
            log.error("Tavily search error: %s", e, exc_info=True)
            return f"Search error: {e}"
    # Tier 2: no Tavily key, but the AgentCore browser is enabled — drive it.
    if _agentcore_browser_enabled():
        log.info("web_search → AgentCore browser fallback: %s", query)
        return (
            "No Tavily key is set, but the Amazon Bedrock AgentCore browser is enabled. "
            "Answer this query by driving that browser: call start_browser_session, then "
            f'browser_navigate to a page that answers "{query}" (a specific authoritative '
            "site, or a search-results URL), read it with browser_evaluate "
            "(e.g. () => document.body.innerText) or browser_snapshot, then call "
            "stop_browser_session. Cite the page you used."
        )
    # Tier 3: nothing set up — guide the user to configure a provider.
    return (
        "Web search isn't set up. Add a Tavily API key in Settings → API Keys, or add and "
        "enable an Amazon Bedrock AgentCore MCP server (browser tools) in Settings → MCP to "
        "search the web using your AWS account."
    )


@register_executor("web_fetch", read_only=True, concurrent_safe=True)
def exec_web_fetch(tool_input, transcript, current_attachments):
    import urllib.error

    url = tool_input.get("url", "").strip()
    if not url:
        return "[error] No URL provided"
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    ok, reason = _is_safe_public_url(url)
    if not ok:
        log.warning("WebFetch blocked %s: %s", url, reason)
        return f"[error] {reason}"

    log.info("WebFetch: %s", url)
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; WhisperStudio/1.0)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        # Build an opener whose redirect handler re-validates every hop, so a
        # public URL that 30x-redirects to an internal address can't slip past
        # the SSRF guard that only saw the original URL.
        opener = urllib.request.build_opener(_SSRFSafeRedirectHandler())
        with opener.open(req, timeout=15) as resp:
            status = resp.status
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read(512_000)

            charset = "utf-8"
            if "charset=" in content_type:
                charset = content_type.split("charset=")[-1].split(";")[0].strip()
            try:
                html = raw.decode(charset)
            except (UnicodeDecodeError, LookupError):
                html = raw.decode("utf-8", errors="replace")

            parser = _HTMLToText()
            parser.feed(html)
            text = parser.get_text()

            import re

            title = ""
            m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
            if m:
                title = m.group(1).strip()

            if len(text) > 30_000:
                text = text[:30_000] + "\n\n[... truncated — page too long]"

            header = f"[Fetched {url}]\nStatus: {status} OK"
            if title:
                header += f"\nTitle: {title}"
            return header + "\n\n" + text

    except _BlockedRedirect as e:
        log.warning("WebFetch blocked redirect to %s: %s", e.url, e.reason)
        return f"[error] blocked redirect to {e.reason}"
    except urllib.error.HTTPError as e:
        log.warning("WebFetch HTTP error: %s %s", e.code, url)
        return f"[Fetched {url}]\nStatus: {e.code} {e.reason}\n\n[HTTP error — page could not be retrieved]"
    except urllib.error.URLError as e:
        log.warning("WebFetch URL error: %s %s", e.reason, url)
        return f"[Fetched {url}]\nStatus: Connection failed\nError: {e.reason}"
    except TimeoutError:
        return f"[Fetched {url}]\nStatus: Timeout\n\n[Request timed out after 15 seconds]"
    except Exception as e:
        log.error("WebFetch error: %s", e, exc_info=True)
        return f"[Fetched {url}]\nStatus: Error\n\n{e}"
