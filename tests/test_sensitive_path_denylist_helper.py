"""is_sensitive_path() — the shared boolean helper new absolute-path callers
(save_file's destination check, its approval executor) reuse instead of
writing their own denylist logic. Must agree with the canonical
expanded_sandbox_paths() list the OS sandbox already draws from."""

import os

from server.security.sensitive_paths import is_sensitive_path


def test_denies_home_credential_paths():
    assert is_sensitive_path(os.path.realpath(os.path.expanduser("~/.ssh/id_rsa"))) is True
    assert is_sensitive_path(os.path.realpath(os.path.expanduser("~/.aws/credentials"))) is True
    assert is_sensitive_path(os.path.realpath(os.path.expanduser("~/.gnupg/private-keys"))) is True


def test_denies_sandbox_only_paths():
    assert is_sensitive_path(os.path.realpath(os.path.expanduser("~/Library/Keychains"))) is True
    assert is_sensitive_path(os.path.realpath(os.path.expanduser("~/.zsh_history"))) is True


def test_denies_absolute_system_paths():
    assert is_sensitive_path("/etc/shadow") is True
    assert is_sensitive_path("/etc/sudoers") is True


def test_denies_nested_paths_under_a_denied_directory():
    nested = os.path.realpath(os.path.expanduser("~/.ssh")) + "/config/extra.conf"
    assert is_sensitive_path(nested) is True


def test_allows_ordinary_paths():
    assert (
        is_sensitive_path(os.path.realpath(os.path.expanduser("~/Documents/report.pdf"))) is False
    )
    assert (
        is_sensitive_path(os.path.realpath(os.path.expanduser("~/Downloads/export.csv"))) is False
    )


def test_does_not_flag_lookalike_names():
    # ~/.awsome must not trip the ~/.aws rule.
    assert is_sensitive_path(os.path.realpath(os.path.expanduser("~/.awsome/notes.txt"))) is False
