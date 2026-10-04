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
        "ag --pager 'touch x' foo",
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


@pytest.mark.parametrize(
    "command",
    [
        # Any remote subcommand but show and get-url rewrites the config; a
        # silent set-url aims the next approved push somewhere else.
        "git remote add evil https://evil/x.git",
        "git remote set-url origin https://evil/x.git",
        "git remote -v set-url origin https://evil/x.git",
        "git remote --verb rm origin",
        "git remote -- remove origin",
        "git remote rename origin upstream",
        "git remote set-head origin -a",
        "git remote set-branches origin main",
        "git remote prune origin",
        "git remote update",
        # An operand outside list mode is a branch or tag to create.
        "git branch feature",
        "git branch -v feature",
        "git branch --format=%(refname) feature",
        "git branch -a feature",
        "git tag v1",
        "git tag --sort=refname v1",
        # Writer flags, short, bundled, abbreviated, or in list mode.
        "git branch -d old",
        "git branch -D old",
        "git branch --del old",
        "git branch -m old new",
        "git branch -M new",
        "git branch -c old new",
        "git branch -C old new",
        "git branch -f main HEAD~1",
        "git branch -u origin/main",
        "git branch --set-upstream-to=origin/main",
        "git branch --unset-upstream",
        "git branch --edit-description",
        "git branch --track topic origin/topic",
        "git branch -t topic origin/topic",
        "git branch --list -d old",
        "git branch -avd old",
        "git tag -d v1",
        "git tag --del v1",
        "git tag -a v1 -m msg",
        "git tag -s v1",
        "git tag -f v1",
        "git tag -F notes.txt v1",
        "git tag -u key v1",
        # uniq writes its second operand; BSD getopt reads a flag after the
        # input as that operand.
        "uniq in.txt out.txt",
        "uniq -c in.txt out.txt",
        "uniq -f 1 in.txt out.txt",
        "uniq in.txt -c",
        "uniq - out.txt",
        "uniq -- in.txt out.txt",
        # sed scripts that write or run a command.
        "sed -n 'w out' file",
        "sed -n '1w out' file",
        "sed -n '/x/W out' file",
        "sed -n 's/a/b/w out' file",
        "sed -n 's/a/b/gw out' file",
        "sed -n 's/a/b/ w out' file",
        "sed -n 's/a/b/e' file",
        "sed -n '1e touch x' file",
        "sed -n -e 1p -e 'w out' file",
        "sed -n --expression='w out' file",
        # GNU ends a label at `;`, BSD at the newline.
        "sed -n 'b end; w out' file",
        # GNU ends the regex at the bracketed `/`, BSD does not.
        "sed -n '/[/]/w out' file",
        "sed -n 's/[/]/w out/' file",
        # A trailing backslash joins the next -e script on.
        "sed -n -e 's/a/b\\' -e '/w out' file",
        # In-place, script-file, and option-after-operand forms.
        "sed -n -I '' 1p file",
        "sed -n -i.bak 1p file",
        "sed -n -f prog.sed file",
        "sed -n 1p file -i",
        "sed -n",
    ],
)
def test_operand_shaped_writers_need_approval(command):
    assert _is_read_only_command(command) is False


@pytest.mark.parametrize(
    "command",
    [
        "git remote",
        "git remote -v",
        "git remote --verbose",
        "git remote show origin",
        "git remote -v show -n origin",
        "git remote get-url --push origin",
        "git branch",
        "git branch -a",
        "git branch -r",
        "git branch -vv",
        "git branch --list",
        "git branch --list 'feat*'",
        "git branch -l 'feat*'",
        "git branch --show-current",
        "git branch --contains abc123",
        "git branch --merged main",
        "git branch --no-merged",
        "git branch --sort=-committerdate",
        "git branch --sort -committerdate",
        "git branch --format='%(refname:short)'",
        "git branch -a --contains HEAD 'feat*'",
        "git tag",
        "git tag -l",
        "git tag --list 'v2.*'",
        "git tag -l 'v2.*' --sort=-v:refname",
        "git tag --contains HEAD",
        "git tag --points-at HEAD",
        "git tag -n3",
        "uniq",
        "uniq -c",
        "uniq -c in.txt",
        "uniq -f 1 in.txt",
        "sort a | uniq -c | sort -rn",
        "sed -n '$p' file",
        "sed -n '$=' file",
        "sed -n '/foo/Ip' file",
        "sed -n '\\|a/b|p' file",
        "sed -n '/start/,/end/p' file",
        "sed -n '/start/,+3p' file",
        "sed -n '0~4p' file",
        "sed -n '5{p;q}' file",
        "sed -n '/x/!p' file",
        "sed -n 's/foo/bar/gp' file",
        "sed -n 's|a/b|c|2p' file",
        "sed -n '/^[[:space:]]*#/p' file",
        "sed -n -e 1p -e '$p' file",
        "sed -n --expression=1p file",
        "sed -n -E 's/(a|b)/x/p' file",
        "sed -n l file",
        "cat log | sed -n '/ERROR/p'",
    ],
)
def test_list_and_print_forms_stay_direct(command):
    assert _is_read_only_command(command) is True
