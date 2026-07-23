"""The sandbox deny-list covers non-credential secret stores (browser cookies,
keychains, shell history, …), and bwrap masks file vs directory entries
correctly."""

import os

from server.sandbox import _DENIED_PATHS, _bwrap_deny_args


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


def test_bwrap_deny_args_dir_vs_file(tmp_path):
    d = tmp_path / "secretdir"
    d.mkdir()
    f = tmp_path / "secret.txt"
    f.write_text("token")

    assert _bwrap_deny_args(str(d)) == ["--tmpfs", str(d)]
    assert _bwrap_deny_args(str(f)) == ["--ro-bind", os.devnull, str(f)]
