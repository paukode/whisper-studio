"""Sensitive-path matching must see through quote characters.

Single (and double) quotes do NOT stop the shell from reading a file, so
`cat '/etc/shadow'` reads the shadow file just like `cat /etc/shadow`. The
expansion/substitution validators legitimately strip whole single-quoted
segments (quoting disables expansion), but the sensitive-path matcher must
strip only the quote CHARACTERS and keep the path text — otherwise quoting a
credential path silently bypasses the denylist.
"""

from server.security.command_validator import (
    check_sensitive_paths,
    validate_command,
)


def test_single_quoted_shadow_is_blocked():
    # Regression: `cat '/etc/shadow'` used to pass because the whole quoted
    # segment (path included) was stripped before matching.
    assert validate_command("cat '/etc/shadow'") is not None
    assert check_sensitive_paths("cat '/etc/shadow'") is not None


def test_double_quoted_shadow_is_blocked():
    assert validate_command('cat "/etc/shadow"') is not None


def test_single_quoted_ssh_key_is_blocked():
    assert validate_command("cat '/Users/me/.ssh/id_rsa'") is not None


def test_double_quoted_ssh_key_is_blocked():
    assert validate_command('cp "/home/me/.ssh/id_ed25519" /tmp/x') is not None


def test_quoted_passwd_is_blocked():
    assert validate_command("grep root '/etc/passwd'") is not None


def test_unquoted_shadow_still_blocked():
    # Baseline: the unquoted form must remain blocked after the change.
    assert validate_command("cat /etc/shadow") is not None


def test_normal_command_with_quoted_nonsensitive_arg_passes():
    # Quoting a benign path must not trip the matcher now that contents are
    # preserved.
    assert validate_command("echo 'hello world'") is None
    assert validate_command("cat './notes/todo.txt'") is None
    assert validate_command('grep -r "some string" src/') is None
