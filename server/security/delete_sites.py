"""Which statements of a shell command delete files, and what they delete.

The rm floor's reader (server.security.delete_scope decides where each
delete reaches). It only marks a command for the stronger approval rule in
server/tool_approvals.py and server/approval/bootstrap.py: always ask a real
human, never satisfied by a session-wide "allow all commands", the auto-mode
classifier, bypassPermissions, or an unattended agent's unconditional
auto-approve. It never blocks a command outright; that is the job of the
deny-list in server.security.command_validator. A command an agent or a
blanket approval would otherwise run silently must still stop for `rm`.
"""

import re
import shlex
from dataclasses import dataclass, field, replace

from server.security.command_validator import split_statements, strip_heredoc_bodies

# The verb itself, plus the closest cousins that destroy data the same way.
# Kept separate from find/xargs invocation below, which needs its own check.
_DELETE_VERBS = frozenset({"rm", "rmdir", "unlink", "shred", "srm"})
# Wrapper commands whose ARGUMENT list eventually names the command actually
# run: strip them so `sudo rm`, `env FOO=bar rm`, `nice -19 rm`, `xargs rm`,
# `nohup rm`, `time rm` are still caught. `env`'s own VAR=value arguments are
# handled by re-running the var-assignment skip after each wrapper, since
# `env` can take several before the real command. A wrapper option that takes
# a value (`sudo -u root rm`, `timeout 10 rm`, `xargs -I {} rm {}`) would hide
# the real command from that skip, so once a wrapper is seen every later word
# is checked too (see _segment_delete_sites).
_INVOKE_WRAPPERS = frozenset(
    {
        "sudo",
        "env",
        "command",
        "nice",
        "exec",
        "doas",
        "xargs",
        "parallel",
        "nohup",
        "time",
        "timeout",
        "setsid",
        "stdbuf",
        "caffeinate",
        "ionice",
        "chrt",
        "taskset",
        "watch",
    }
)
# Shell reserved words that start a statement whose command follows them
# (`if rm x; then`, `then rm -rf dist; fi`): skipped like a wrapper, and
# everything after them is checked. A subshell's opening paren written as a
# word of its own (`( rm -rf x )`) starts one the same way. `case` and
# `function` put a word or two before the command they lead to (`case $x in
# a) rm -rf x`, `function f { rm -rf x; }`), which the check of every later
# word finds.
_SHELL_KEYWORDS = frozenset(
    {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "(", "case", "function"}
)
# zsh's precommand modifiers and the builtin keyword: each runs the command
# after it in this same shell (`noglob rm -rf x`, `builtin cd web`).
_PRECOMMANDS = frozenset({"builtin", "noglob", "nocorrect"})
# A function definition (`f() { ...; }`, `f () cmd`, zsh's `f g () cmd`): an
# unquoted pair of parens with nothing between them. Quoted text and escaped
# characters are taken out before the search.
_FUNCTION_PARENS = re.compile(r"\(\s*\)")
_QUOTED_TEXT = re.compile(r"""'[^']*'|"(?:[^"\\]|\\.)*"|\\.""", re.DOTALL)
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Shells whose -c argument is a whole script of its own (`sh -c "rm -rf x"`,
# `bash -lc '...'`): the script is checked like any other command line.
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "fish", "csh", "tcsh"})
# Shell options that consume the next word (`bash -o pipefail -c ...`).
_SHELL_LONG_OPTS_WITH_VALUE = frozenset({"--rcfile", "--init-file"})
# Scripts nested deeper than this (`sh -c "bash -c '...'"`) are treated as
# rm-like: fail toward asking.
_MAX_SCRIPT_DEPTH = 4
# find's own -delete action, and the common find ... -exec rm pattern, destroy
# files without the literal token "rm" ever being find's own argv[0].
_FIND_DELETE_RE = re.compile(r"\bfind\b.*?(-delete\b|-exec\s+rm\b)")
# `cd` (and zsh's `chdir`) moves every later statement of the same shell; its
# -L/-P/-e/-@ options say how it follows links, not where it goes.
_CD_VERBS = frozenset({"cd", "chdir"})
_CD_OPTION = re.compile(r"^-[LPe@]+$")
# pushd and popd move to a directory the command line does not name: popd's
# comes off the directory stack.
_DIRSTACK_VERBS = frozenset({"pushd", "popd"})
# A redirection word (`2>/dev/null`, `>`, `<<<`) is never a cd operand. One
# with nothing after its operator takes the next word as its target.
_REDIRECTION = re.compile(r"^(?:\d+|&)?(?:>&|<&|<<<|>>|>\||>|<)(.*)$")
# Words that start a loop. Its body runs again after its own later
# statements, so a directory change late in the body moves a delete that
# comes before it on the next pass. A function body runs the same way, at
# each call.
_LOOP_WORDS = frozenset({"for", "while", "until", "select", "repeat", "foreach", "do"})


@dataclass(frozen=True)
class _WrapperOptions:
    """The options a wrapper reads before the command it runs: whether each
    short letter takes a value, and each long name's letter ("" when it has
    none) and whether it takes a value. getopt_long also accepts a unique
    prefix of a long name. ``chdirs`` move the command to the directory
    they name, ``homes`` to the target user's home, ``roots`` under another
    root directory."""

    short: dict[str, bool]
    long: dict[str, tuple[str, bool]]
    chdirs: frozenset[str] = frozenset()
    homes: frozenset[str] = frozenset()
    roots: frozenset[str] = frozenset()


# env (BSD and GNU) and sudo, the wrappers that can move the command they run
# (`env -C dir rm`, `sudo -D dir rm`), with every option they have, so the
# words before the command are read exactly (`sudo -u root -nD dir rm`).
_WRAPPER_OPTIONS = {
    "env": _WrapperOptions(
        short={**dict.fromkeys("0iv", False), **dict.fromkeys("CLPSUu", True)},
        long={
            "block-signal": ("", False),
            "chdir": ("C", True),
            "debug": ("v", False),
            "default-signal": ("", False),
            "help": ("", False),
            "ignore-environment": ("i", False),
            "ignore-signal": ("", False),
            "list-signal-handling": ("", False),
            "null": ("0", False),
            "split-string": ("S", True),
            "unset": ("u", True),
            "version": ("", False),
        },
        chdirs=frozenset("C"),
    ),
    "sudo": _WrapperOptions(
        short={**dict.fromkeys("ABbEeHhiKklNnPSsVv", False), **dict.fromkeys("aCcDgpRrTtUu", True)},
        long={
            "askpass": ("A", False),
            "auth-type": ("a", True),
            "background": ("b", False),
            "bell": ("B", False),
            "chdir": ("D", True),
            "chroot": ("R", True),
            "close-from": ("C", True),
            "command-timeout": ("T", True),
            "edit": ("e", False),
            "group": ("g", True),
            "help": ("h", False),
            "host": ("", True),
            "list": ("l", False),
            "login": ("i", False),
            "login-class": ("c", True),
            "non-interactive": ("n", False),
            "other-user": ("U", True),
            "preserve-env": ("E", False),
            "preserve-groups": ("P", False),
            "prompt": ("p", True),
            "remove-timestamp": ("K", False),
            "reset-timestamp": ("k", False),
            "role": ("r", True),
            "set-home": ("H", False),
            "shell": ("s", False),
            "stdin": ("S", False),
            "type": ("t", True),
            "user": ("u", True),
            "validate": ("v", False),
            "version": ("V", False),
        },
        chdirs=frozenset("D"),
        homes=frozenset("i"),
        roots=frozenset("R"),
    ),
}


def _shell_script(tokens: list[str], i: int) -> str | None:
    """The script a shell at ``tokens[i]`` runs with -c, or None when it has
    no -c (``bash build.sh`` runs a file this check cannot see)."""
    has_c = False
    j = i + 1
    while j < len(tokens):
        tok = tokens[j]
        if tok == "--":
            j += 1
            break
        if tok.startswith("--"):
            j += 2 if tok in _SHELL_LONG_OPTS_WITH_VALUE else 1
            continue
        if len(tok) > 1 and tok[0] in "-+":
            flags = tok[1:]
            has_c = has_c or "c" in flags
            # -o option / +O shopt_name: the option cluster ends in a letter
            # that takes the next word as its value.
            j += 2 if flags[-1] in "oO" else 1
            continue
        break
    if has_c and j < len(tokens):
        return tokens[j]
    return None


def shell_scripts(argv: list[str]) -> list[str]:
    """The scripts an argv hands to a shell with -c, wherever the shell sits in
    it (``["nohup", "bash", "-lc", "rm -rf dist"]``). For a command that is
    exec'd without a shell, so its arguments were never one command line: the
    rm check reads each script as a command line of its own."""
    scripts = []
    for i, tok in enumerate(argv):
        if _base(tok) in _SHELLS:
            script = _shell_script(argv, i)
            if script is not None:
                scripts.append(script)
    return scripts


def _base(token: str) -> str:
    """A word's command name: its basename, past a subshell's opening paren."""
    return token.lstrip("(").rsplit("/", 1)[-1]


@dataclass(frozen=True)
class ChDir:
    """A directory change a statement makes for the statements after it.
    ``target`` is the ``cd`` operand as written, or None when the command
    line does not name one (``cd -``, a bare ``cd``, ``pushd``, ``popd``); a
    target with a variable in it is passed on as written, for the caller to
    find unresolvable like any other operand. ``sure`` when the delete it
    precedes runs only after it succeeded, in the same shell: the cd starts a
    statement of its own and only ``&&`` joins it to the delete. Otherwise
    the delete may run where it was (the cd failed before a ``;``, ran in a
    pipeline stage or a subshell, or sits behind ``||``)."""

    target: str | None
    sure: bool = False


@dataclass(frozen=True)
class DeleteSite:
    """One deleting statement in a command: its verb and the operands it would
    delete. ``operands`` is None when the command line alone cannot say what
    gets deleted (paths fed by xargs or parallel, a segment that does not
    tokenize, a script nested too deep); a caller deciding where the delete
    reaches must treat that as anywhere. ``chdirs`` are the directory changes
    before it, in order (an outer command line's before those of the -c
    script it runs)."""

    verb: str
    operands: list[str] | None
    chdirs: tuple[ChDir, ...] = ()


@dataclass
class _Walk:
    """What the statements read so far do to the shell's working directory.
    A -c script or an ``eval`` text is read with a walk of its own that
    starts from its caller's changes (``inherited`` of them): its own
    operators decide only about its own changes."""

    chdirs: list[ChDir] = field(default_factory=list)
    inherited: int = 0
    moved: bool = False  # a statement of this walk changed directory
    loops: bool = False  # a statement of this walk starts a loop or defines a function

    def child(self, moves: tuple[ChDir, ...] = ()) -> "_Walk":
        """A walk for a script run from here, after ``moves``: the changes
        the wrappers that run it make (_wrapper_moves)."""
        chdirs = [*self.chdirs, *moves]
        return _Walk(chdirs, len(chdirs))

    def unsure(self) -> None:
        """The chain of ``&&`` from this walk's changes is broken."""
        own = self.chdirs[self.inherited :]
        self.chdirs[self.inherited :] = [ChDir(c.target) for c in own]


_UNKNOWN_SITE = DeleteSite("?", None)
# What may come before a statement for a cd in it to be sure: nothing, or an
# operator that runs it in the current shell whatever ran before (not `||`,
# which can skip it, nor a pipe, which can run it in a subshell).
_CHAIN_STARTS = frozenset({"", ";", "&", "&&"})
# A paren anywhere in a statement may open or close a subshell, whose cd
# ends with it.
_SUBSHELL_CHARS = frozenset("()")
# Wrappers that feed the command its operands on stdin, so the operands are
# not on the command line.
_STDIN_FEEDERS = frozenset({"xargs", "parallel"})
# find's own options that precede its starting points (BSD and GNU); -f and
# -D take the next word.
_FIND_LEADING_OPTS = re.compile(r"^-[HLPEXdsx]+$")
_FIND_OPTS_WITH_VALUE = frozenset({"-f", "-D"})
_FIND_EXEC_ACTIONS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})


def _delete_operands(tokens: list[str], j: int) -> list[str]:
    """What the delete verb at ``tokens[j]`` deletes: every later word that is
    not an option. ``--`` ends the options, as it does for rm."""
    operands: list[str] = []
    options_done = False
    for tok in tokens[j + 1 :]:
        if not options_done and tok == "--":
            options_done = True
            continue
        if not options_done and len(tok) > 1 and tok.startswith("-"):
            continue
        operands.append(tok)
    return operands


def _find_site(tokens: list[str], j: int) -> DeleteSite | None:
    """The delete a ``find`` at ``tokens[j]`` performs through ``-delete`` or
    ``-exec rm``, with the starting points it walks as the operands (the
    working directory when it names none), or None when it deletes nothing."""
    rest = tokens[j + 1 :]
    deletes = "-delete" in rest or any(
        a in _FIND_EXEC_ACTIONS and _base(b) in _DELETE_VERBS
        for a, b in zip(rest, rest[1:], strict=False)
    )
    if not deletes:
        return None
    paths: list[str] = []
    k = 0
    while k < len(rest):
        tok = rest[k]
        if tok in _FIND_OPTS_WITH_VALUE:
            if tok == "-f" and k + 1 < len(rest):
                paths.append(rest[k + 1])
            k += 2
            continue
        if _FIND_LEADING_OPTS.match(tok):
            k += 1
            continue
        if tok.startswith(("-", "(", "!", ",")):
            break
        paths.append(tok)
        k += 1
    return DeleteSite("find", paths or ["."])


def _script_delete_sites(tokens: list[str], j: int, depth: int, walk: _Walk) -> list[DeleteSite]:
    """The deletes inside the script a shell at ``tokens[j]`` runs with -c, or
    the arguments an ``eval`` there runs."""
    base = _base(tokens[j])
    if base == "eval":
        # eval runs its text in this shell, so its cds stay for what follows.
        text = walk.child()
        sites = _delete_sites(" ".join(tokens[j + 1 :]), depth + 1, text)
        walk.chdirs[:] = text.chdirs
        walk.moved = walk.moved or text.moved
        walk.loops = walk.loops or text.loops
        return sites
    if base in _SHELLS:
        script = _shell_script(tokens, j)
        if script is None:
            return []
        moves = _wrapper_moves(tokens, j)
        sites = _delete_sites(script, depth + 1, walk.child(moves or ()))
        return sites if moves is not None else [replace(s, operands=None) for s in sites]
    return []


def _attached_value(value: str) -> str | None:
    """An option value written onto its option (``-Ddir``, ``--chdir=dir``).
    The shell expands ``~`` only at the start of a word, so a value that
    starts with one names a folder called ``~``; read as unknown."""
    return value if value and not value.startswith("~") else None


def _wrapper_options(
    tokens: list[str], w: int, spec: _WrapperOptions
) -> tuple[list[tuple[str, str | None]], int] | None:
    """The options the wrapper at ``tokens[w]`` reads, as (letter, value)
    pairs, and the index of the first word after them (its command, or an
    assignment before it). ``-D dir``, ``-nDdir`` and ``--chdir=dir`` all
    read as ("D", "dir"); a value missing at the end of the line is None.
    Redirections between the options are passed over. None when a word is
    an option the wrapper does not have, or a long name that is not one of
    its names or a unique prefix of one."""
    found: list[tuple[str, str | None]] = []
    k = w + 1
    while k < len(tokens):
        word = tokens[k]
        redirection = _REDIRECTION.match(word)
        if redirection:
            k += 1 if redirection.group(1) else 2
            continue
        if word == "--" or not word.startswith("-"):
            break
        if word.startswith("--"):
            name, eq, value = word[2:].partition("=")
            names = [name] if name in spec.long else [n for n in spec.long if n.startswith(name)]
            if len(names) != 1:
                return None
            letter, takes_value = spec.long[names[0]]
            if takes_value and not eq:
                k += 1
                found.append((letter, tokens[k] if k < len(tokens) else None))
            else:
                found.append((letter, _attached_value(value) if takes_value else None))
            k += 1
            continue
        letters = word[1:]
        for n, letter in enumerate(letters):
            if letter not in spec.short:
                return None
            if not spec.short[letter]:
                found.append((letter, None))
                continue
            if letters[n + 1 :]:
                found.append((letter, _attached_value(letters[n + 1 :])))
            else:
                k += 1
                found.append((letter, tokens[k] if k < len(tokens) else None))
            break
        k += 1
    return found, k


def _wrapper_moves(tokens: list[str], j: int) -> tuple[ChDir, ...] | None:
    """The directory changes the wrappers before ``tokens[j]`` make for the
    command they run, in order. ``env -C`` and ``sudo -D`` move it to the
    directory they name (None when the command line does not name one, see
    _wrapper_options); ``sudo -i`` moves it to the target user's home, which
    the command line does not name. Each is sure: when the move fails, the
    wrapper does not run the command. A wrapper given an option it does not
    have moves it somewhere unknown. None when a wrapper runs the command
    under another root (``sudo -R``): no path it names reads as written."""
    moves: list[ChDir] = []
    w = 0
    while w < j:
        spec = _WRAPPER_OPTIONS.get(_base(tokens[w]))
        read = _wrapper_options(tokens, w, spec) if spec is not None else None
        if read is None:
            if spec is not None:
                moves.append(ChDir(None, sure=True))
            w += 1
            continue
        options, w = read  # an option's value is never a wrapper of its own
        for letter, value in options:
            if letter in spec.roots:
                return None
            if letter in spec.chdirs:
                moves.append(ChDir(value, sure=True))
            elif letter in spec.homes:
                moves.append(ChDir(None, sure=True))
    return tuple(moves)


def _placed(site: DeleteSite, tokens: list[str], j: int, chdirs: tuple[ChDir, ...]) -> DeleteSite:
    """``site``, the delete whose verb is ``tokens[j]``, with the directory
    changes before it: ``chdirs`` from the statements before, then those its
    wrappers make. Under another root its operands cannot be read."""
    moves = _wrapper_moves(tokens, j)
    if moves is None:
        return DeleteSite(site.verb, None, chdirs)
    return replace(site, chdirs=chdirs + moves)


def _cd_target(args: list[str]) -> str | None:
    """The directory ``cd args`` moves to, as written, or None when the words
    do not name exactly one: no operand (home), ``-`` (the previous
    directory), a stack entry (``-2``, ``+1``), an option cd does not have,
    or zsh's two-operand form, which edits the current path."""
    words: list[str] = []
    k = 0
    while k < len(args):
        redirection = _REDIRECTION.match(args[k])
        if redirection:
            k += 1 if redirection.group(1) else 2
            continue
        words.append(args[k])
        k += 1
    while words and _CD_OPTION.match(words[0]):
        words.pop(0)
    if words and words[0] == "--":
        words.pop(0)
    if len(words) != 1 or re.match(r"^[-+]", words[0]):
        return None
    return words[0]


def _record_chdir(tokens: list[str], j: int, walk: _Walk, chained: bool) -> None:
    """Note the directory change a ``cd`` at ``tokens[j]`` makes for the
    statements after it, if the word there is one. It is sure only when the
    statement is ``chained`` and nothing but a precommand modifier comes
    before the cd: a wrapper can run it in a child process, ``!`` turns its
    failure into success, a case arm or a function body may never run."""
    verb = _base(tokens[j])
    if verb in _CD_VERBS:
        target = _cd_target(tokens[j + 1 :])
    elif verb in _DIRSTACK_VERBS:
        target = None
    else:
        return
    sure = chained and all(t in _PRECOMMANDS for t in tokens[:j])
    walk.chdirs.append(ChDir(target, sure))
    walk.moved = True


def _leads_to_command(word: str) -> bool:
    """Whether ``word`` comes before a statement's command rather than being
    it: an invocation wrapper, a reserved word, a precommand modifier, or a
    word that ends in a paren (a case arm's pattern such as ``a)`` or
    ``(a|b)``, a function's name with its parens such as ``f()``)."""
    return (
        _base(word) in _INVOKE_WRAPPERS
        or word in _SHELL_KEYWORDS
        or word in _PRECOMMANDS
        or word.endswith(")")
    )


def _command_index(tokens: list[str]) -> tuple[int, bool]:
    """Where a statement's command sits, past environment-variable
    assignments (``FOO=bar rm``), flags, and the words that lead to it
    (_leads_to_command), and whether any such word was passed: a wrapper
    option's value can sit where the command's name would be, so every
    later word is checked then too."""

    def skip_assignments_and_flags(i: int) -> int:
        while i < len(tokens) and (_ASSIGNMENT.match(tokens[i]) or tokens[i].startswith("-")):
            i += 1
        return i

    i = skip_assignments_and_flags(0)
    led = False
    while i < len(tokens) and _leads_to_command(tokens[i]):
        led = True
        i = skip_assignments_and_flags(i + 1)  # the wrapper's own flags/env args
    return i, led


def _segment_delete_sites(
    segment: str, depth: int, walk: _Walk | None = None, chained: bool = False
) -> list[DeleteSite]:
    """The deletes one pipeline stage performs. Its leading command (past
    environment-variable assignments such as ``FOO=bar rm``, invocation
    wrappers, reserved words, zsh's precommand modifiers and a case arm's
    pattern, see _command_index) is a delete verb, a ``find`` that deletes,
    or a shell/eval running a deleting script. Behind any of those every
    later word is checked as well, since a wrapper option's value can sit
    where the command's name would be. A function definition is checked the
    same way: its body can follow any number of names.

    A segment that cannot be tokenized (unbalanced quotes) is a delete whose
    reach is unknown: a parse failure is exactly the kind of thing an
    obfuscated command produces. A segment that runs nothing at all (a bare
    ``x=1``, an option cluster, a reserved word on its own line) deletes
    nothing: every shell script is full of those.

    Each delete carries the directory changes ``walk`` holds when the segment
    starts, then those its own wrappers make (``env -C``, ``sudo -D``); a
    ``cd`` among the checked words is added to ``walk`` for the segments
    after it (``chained``: the operator before the segment lets the cd be
    sure, see ChDir)."""
    walk = walk if walk is not None else _Walk()
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        return [_UNKNOWN_SITE]

    i, wrapped = _command_index(tokens)
    defines = "function" in tokens[:i] or bool(
        _FUNCTION_PARENS.search(_QUOTED_TEXT.sub("", segment))
    )
    walk.loops = walk.loops or defines or any(_base(t) in _LOOP_WORDS for t in tokens[: i + 1])
    if i >= len(tokens):
        return []
    chdirs = tuple(walk.chdirs)
    last = len(tokens) if wrapped or defines else i + 1
    sites: list[DeleteSite] = []
    for j in range(i, last):
        base = _base(tokens[j])
        if base in _DELETE_VERBS:
            fed = any(_base(t) in _STDIN_FEEDERS for t in tokens[:j])
            operands = None if fed else _delete_operands(tokens, j)
            sites.append(_placed(DeleteSite(base, operands), tokens, j, chdirs))
            break
        if base == "find":
            site = _find_site(tokens, j)
            if site is not None:
                sites.append(_placed(site, tokens, j, chdirs))
                break
        sites.extend(_script_delete_sites(tokens, j, depth, walk))
        _record_chdir(tokens, j, walk, chained)
    return sites


def _delete_sites(command: str, depth: int, walk: _Walk | None = None) -> list[DeleteSite]:
    if depth > _MAX_SCRIPT_DEPTH:
        return [_UNKNOWN_SITE]
    walk = walk if walk is not None else _Walk()
    command = strip_heredoc_bodies(command)
    sites: list[DeleteSite] = []
    # _statements splits as split_subcommands does: on every UNQUOTED ;, &
    # and | (pipeline stages included), not on bare newlines: a multi-line
    # script has no separator between lines at all, so without the line
    # split a line merely following an rm-free first line would never be
    # inspected on its own. A | inside quotes is data, never a pipe:
    # splitting there again (a regex like 'a|b') left each piece with an
    # unclosed quote, and a parse failure reads as a delete of unknown reach.
    # The operators around each statement say whether a cd before a delete
    # surely ran (ChDir.sure). A line ending in an operator (`cd x &&`)
    # carries it to the next line's first statement.
    carried = ""
    for line in command.split("\n"):
        for before, segment, after in split_statements(line):
            chained = (before or carried) in _CHAIN_STARTS
            sites.extend(_segment_delete_sites(segment, depth, walk, chained))
            if after != "&&" or _SUBSHELL_CHARS & set(segment):
                walk.unsure()
            carried = after
    # A find that deletes in a shape the token walk did not read (fail
    # toward asking): the delete is real, its reach unknown.
    if _FIND_DELETE_RE.search(command) and not any(s.verb == "find" for s in sites):
        sites.append(_UNKNOWN_SITE)
    # A loop runs a directory change again, before the deletes that precede
    # it in the body and more than once for the ones after it, so a line
    # that has both leaves no delete in it a working directory it can name.
    if walk.loops and walk.moved:
        sites = [replace(s, chdirs=(ChDir(None),)) for s in sites]
    return sites


def delete_sites(command: str) -> list[DeleteSite]:
    """Every statement in this shell command that deletes files, with what it
    deletes: rm and its close cousins directly, `sudo`/`env`/`nohup`/`time`/
    etc.-wrapped, piped, chained with ;/&&/||, on its own line with no
    separator (a multi-line terminal_run/workflow script), inside a shell's
    -c script (`sh -c "rm -rf build"`, `bash -lc '...'`) or an `eval`, or
    find's own -delete / -exec rm. A parse failure on any segment (unbalanced
    quotes, the kind of thing a disguised command produces) is a delete of
    unknown reach: fail toward requiring approval, not away from it. Each
    delete also carries the ``cd``s that run before it (DeleteSite.chdirs).

    Heredoc bodies are stripped first: they are literal data (e.g. a file
    being written), not commands, so text that merely CONTAINS the word "rm"
    must not trip this.
    """
    return _delete_sites(command, 0)


def is_rm_command(command: str) -> bool:
    """True if any statement in this shell command deletes files (see
    delete_sites)."""
    return bool(delete_sites(command))
