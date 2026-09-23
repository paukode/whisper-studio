"""The read-only classifier decides which ws_run_command calls run with no
approval card, so it must fail closed: any shell syntax that can run a second
command, and any argument or redirect that writes a file or launches another
program, needs approval. Genuine read-only commands stay direct."""

import pytest

from server.workspace.executors import _is_read_only_command


@pytest.mark.parametrize(
    "command",
    [
        # Control operators chain a second command after an allowlisted one.
        "echo hi && touch x",
        "cat a && npm install",
        "ls ; touch x",
        "ls;touch x",
        "ls & touch x",
        "ls\ntouch x",
        "git status || rm -rf build",
        # Substitutions run code wherever they appear, double quotes included.
        "ls $(touch x)",
        "ls `touch x`",
        'echo "$(touch x)"',
        'echo "`touch x`"',
        "cat <(touch x)",
        "diff a >(touch x)",
        "echo ${x:-$(touch y)}",
        # Subshells, brace groups, and zsh glob qualifiers.
        "(touch x)",
        "ls *(e:'touch x':)",
        "{ touch x; }",
        # A lexer that got quoting wrong would see these as harmless.
        "echo \\' > out \\'",
        "echo $'\\'' ; touch x '",
        "ls 'unterminated",
        "ls \\",
    ],
)
def test_chained_and_substituted_commands_need_approval(command):
    assert _is_read_only_command(command) is False


@pytest.mark.parametrize(
    "command",
    [
        # Allowlisted readers that run whatever they are handed.
        "env touch x",
        "env -i sh -c 'touch x'",
        "rg --pre=sh foo",
        "rg --pre sh foo",
        "rg --hostname-bin=./x --hyperlink-format=default foo",
        "find . -exec rm {} ;",
        "find . '-delete'",
        # Readers with a flag that writes a file.
        "sort -o out.txt in.txt",
        "sort -uo out.txt in.txt",
        "sort --out=out.txt in.txt",
        "sed -n -i '' 1p file",
        "git log --output=log.txt",
        "git diff --out=d.txt",
        "find . -fprint0 list",
        "tree -o listing.txt",
        "file -C -m magic",
        # Redirects to anything but /dev/null, or to a bash network socket.
        "echo pwned > ~/.bashrc",
        "printf x >> file",
        "ls >&1x",
        "ls &>out.txt",
        "cat <>file",
        "cat < /dev/tcp/example.com/80",
        "ls >",
    ],
)
def test_writers_and_launchers_need_approval(command):
    assert _is_read_only_command(command) is False


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "git status",
        "grep -rn foo . | head",
        "git status && git log --oneline -5",
        "ls missing || echo none",
        "cd src && ls",
        "grep 'a|b;c&d' file",
        'grep "foo$" file',
        "echo $HOME",
        "grep foo bar 2>/dev/null",
        "tail -f log 2>&1",
        "ls >/dev/null 2>&1",
        "ls &>/dev/null",
        "sort < names.txt",
        "sort -rn counts.txt",
        "rg --pre-glob '*.gz' foo",
        "sed -n '10,20p' file",
        "find . -name '*.py'",
        "git log --oneline",
        "env",
        "env | grep PATH",
    ],
)
def test_genuine_read_only_commands_run_directly(command):
    assert _is_read_only_command(command) is True


def test_a_leading_cd_needs_a_command_after_it():
    # Only a leading `cd DIR` is skipped; on its own, or chained to a writer,
    # it still needs approval.
    assert _is_read_only_command("cd /tmp") is False
    assert _is_read_only_command("cd /tmp && rm x") is False
    assert _is_read_only_command("ls && cd /tmp && ls") is False
