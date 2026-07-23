---
name: web_fetch
description: Fetches a single public web page over HTTP(S) and returns its text as a header (URL, HTTP status, title) plus the content converted from HTML to plain text with heading and list markers, truncated at 30,000 characters. Use when the user provides a URL or a specific known page must be read. Use web_search first when the page must be found. Limits are a 15 second timeout and no JavaScript rendering, so script-heavy pages may return little text; localhost, private, and cloud-metadata addresses are blocked; https is assumed when the scheme is missing; HTTP errors are reported in the Status line instead of failing. Not for local files (use ws_read_file) or interactive pages.
triggers: fetch, url, website, read page, open link, fetch page, whats on this link, http, www
executor: web_fetch
input_schema:
  url:
    type: string
    required: true
    description: "The full URL to fetch. https:// is assumed if no scheme is given."
---

# Web Fetch

Executor-backed tool. This body is documentation for the Skills panel; the model
sees only the frontmatter description and input_schema. Behavior at runtime:

- Fetches the URL over HTTP(S) (15s timeout), converts the HTML to plain text with
  heading and list markers, and returns a header (URL, status, title) plus the text,
  truncated at 30,000 characters.
- SSRF-guarded: localhost, private (RFC1918), link-local, and cloud-metadata
  (169.254.169.254) addresses are refused. Only http and https schemes are allowed.
- No JavaScript is executed, so single-page apps that render client-side may return
  little text. HTTP errors (404, 500) are reported in the Status line, not raised.

Use web_search to find a page first when the URL is unknown; use ws_read_file for
local files.
