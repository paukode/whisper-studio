"""The HTML-to-text converter behind web_fetch promises readable markdown:
<a href> must come out as a balanced [text](href) link, never a stray "[",
and anchor-only or javascript: hrefs must stay plain text."""

from server.executors.web import _HTMLToText


def convert(html: str) -> str:
    parser = _HTMLToText()
    parser.feed(html)
    return parser.get_text()


def test_link_becomes_markdown():
    out = convert('<p>See <a href="https://example.com/docs">the docs</a> for more.</p>')
    assert "[the docs](https://example.com/docs)" in out


def test_multiple_links_stay_balanced():
    out = convert(
        '<p><a href="https://a.example/">one</a> and <a href="https://b.example/">two</a></p>'
    )
    assert "[one](https://a.example/)" in out
    assert "[two](https://b.example/)" in out
    assert out.count("[") == out.count("]")


def test_anchor_and_javascript_hrefs_are_plain_text():
    out = convert('<a href="#section">jump</a> <a href="javascript:void(0)">click</a>')
    assert "jump" in out
    assert "click" in out
    assert "[" not in out
    assert "(" not in out


def test_link_without_href_is_plain_text():
    out = convert("<a>bare</a>")
    assert out == "bare"


def test_unclosed_link_still_balances():
    out = convert('<p><a href="https://example.com/">dangling</p>')
    assert out.count("[") == out.count("]")
    assert "(https://example.com/)" in out


def test_nested_links_close_in_order():
    out = convert('<a href="https://outer.example/">out<a href="https://inner.example/">in</a></a>')
    assert "[in](https://inner.example/)" in out
    assert out.count("[") == out.count("]")
