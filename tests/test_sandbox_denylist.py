"""The sandbox deny-list covers non-credential secret stores (browser cookies,
keychains, shell history, …)."""

import os

from server.sandbox import _DENIED_PATHS


def test_denylist_covers_new_secret_stores():
    denied = set(_DENIED_PATHS)
    expected = [
        os.path.expanduser("~/Library/Keychains"),
        os.path.expanduser("~/Library/Cookies"),
        os.path.expanduser("~/.zsh_history"),
        os.path.expanduser("~/.password-store"),
        os.path.expanduser("~/.config/git/credentials"),
    ]
    for p in expected:
        assert p in denied, f"{p} should be on the deny-list"
    # ~/.aws stays denied by default (AWS tools re-allow it per-call, not by removal).
    assert os.path.expanduser("~/.aws") in denied
