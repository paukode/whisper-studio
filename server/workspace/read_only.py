"""Classifier that decides whether ws_run_command may skip the approval card.

It fails closed: a command runs without approval only when it is provably
read-only. The command is lexed the way /bin/sh (and zsh, which the shell
snapshot may re-exec under) splits it, then every segment between control
operators must start with an allowlisted reader and carry no argument or
redirect that writes a file or launches another program. Shell syntax the
lexer does not model (substitution, subshells, newlines) needs approval.
"""

_READ_ONLY_COMMAND_PREFIXES = frozenset(
    {
        "git status",
        "git diff",
        "git log",
        "git show",
        "git branch",
        "git tag",
        "git remote",
        "git stash list",
        "git rev-parse",
        "git describe",
        "git shortlog",
        "git blame",
        "git ls-files",
        "git ls-tree",
        "ls",
        "cat",
        "head",
        "tail",
        "wc",
        "file",
        "stat",
        "du",
        "df",
        "find",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
        "echo",
        "printf",
        "date",
        "whoami",
        "hostname",
        "uname",
        "pwd",
        "which",
        "where",
        "type",
        "env",
        "printenv",
        "diff",
        "cmp",
        "sort",
        "uniq",
        "tr",
        "cut",
        "sed -n",
        "tree",
        "readlink",
        "realpath",
        "basename",
        "dirname",
    }
)
_READ_ONLY_COMMANDS = frozenset(tuple(p.split()) for p in _READ_ONLY_COMMAND_PREFIXES)

# `find` action predicates that write or execute: their presence turns an
# otherwise read-only `find` into a mutation or arbitrary-exec path.
_FIND_WRITE_ACTIONS = frozenset(
    {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
)

# Flags that turn an allowlisted reader into a writer or a launcher of another
# program. A short flag also matches inside a bundle (`-uo`); a long flag
# matches every abbreviation getopt and git accept (`--out` for --output).
# `env` is handled separately: any operand is a command it runs.
_WRITE_OR_EXEC_FLAGS: dict[tuple[str, ...], tuple[str, ...]] = {
    ("rg",): ("--pre", "--hostname-bin"),
    ("sort",): ("-o", "--output", "--compress-program"),
    ("sed", "-n"): ("-i", "-f", "--in-place", "--file"),
    ("tree",): ("-o", "-R"),
    ("file",): ("-C", "--compile"),
    ("git", "diff"): ("--output",),
    ("git", "log"): ("--output",),
    ("git", "show"): ("--output",),
    ("git", "stash", "list"): ("--output",),
}

# Longest first, so `&&` is not read as two `&`.
_OPERATORS = (
    "&>>",
    "<<<",
    "<<-",
    "&&",
    "||",
    "|&",
    ">>",
    ">&",
    ">|",
    "<<",
    "<&",
    "<>",
    "&>",
    ";",
    "&",
    "|",
    "<",
    ">",
)
_CONTROL_OPERATORS = frozenset({";", "&", "&&", "||", "|", "|&"})


def _shell_tokens(command: str) -> list[tuple[str, str]] | None:
    """Lex ``command`` into ("word", text) and ("op", operator) tokens.

    Words come back with their quotes removed. Returns None for syntax that
    can run code the words do not show: command, process, or parameter
    substitution (``$(``, ``${``, ``$[``, backticks, also inside double
    quotes), ``$'...'`` quoting, parentheses (subshells, zsh glob
    qualifiers), newlines outside quotes, a trailing backslash, and
    unterminated quotes.
    """
    tokens: list[tuple[str, str]] = []
    word: list[str] | None = None
    i, n = 0, len(command)

    def flush() -> None:
        nonlocal word
        if word is not None:
            tokens.append(("word", "".join(word)))
            word = None

    while i < n:
        c = command[i]
        if c in " \t":
            flush()
            i += 1
        elif c in "\n`()":
            return None
        elif c == "$" and command[i + 1 : i + 2] in ("(", "{", "[", "'", '"'):
            return None
        elif c == "\\":
            if i + 1 >= n or command[i + 1] == "\n":
                return None
            word = (word or []) + [command[i + 1]]
            i += 2
        elif c == "'":
            end = command.find("'", i + 1)
            if end == -1:
                return None
            word = (word or []) + [command[i + 1 : end]]
            i = end + 1
        elif c == '"':
            word = word or []
            i += 1
            while True:
                if i >= n:
                    return None
                d = command[i]
                if d == '"':
                    i += 1
                    break
                if d == "`" or (d == "$" and command[i + 1 : i + 2] in ("(", "{", "[")):
                    return None
                if d == "\\" and command[i + 1 : i + 2] in ("$", "`", '"', "\\"):
                    word.append(command[i + 1])
                    i += 2
                    continue
                if d == "\\" and command[i + 1 : i + 2] == "\n":
                    return None
                word.append(d)
                i += 1
        elif c in ";&|<>":
            # An all-digit word right before a redirect is its fd (`2>`).
            if c in "<>" and word is not None and "".join(word).isdigit():
                word = None
            flush()
            op = next(op for op in _OPERATORS if command.startswith(op, i))
            tokens.append(("op", op))
            i += len(op)
        else:
            word = (word or []) + [c]
            i += 1
    flush()
    return tokens


def _redirect_is_safe(op: str, target: str) -> bool:
    """Reads, fd merges, and /dev/null sinks are safe; any other output
    target is a file write, and a /dev path can be a bash network socket."""
    if op in ("<<", "<<-", "<<<"):
        return True  # heredoc delimiter or herestring: no file is opened
    if op in (">&", "<&") and (target.isdigit() or target == "-"):
        return True
    if target == "/dev/null":
        return True
    return op == "<" and not target.startswith("/dev/")


def _split_segments(tokens: list[tuple[str, str]]) -> list[list[str]] | None:
    """Group words into segments between control operators, validating and
    dropping each redirect. None when a redirect is unsafe or has no target."""
    segments: list[list[str]] = [[]]
    stream = iter(tokens)
    for kind, text in stream:
        if kind == "word":
            segments[-1].append(text)
        elif text in _CONTROL_OPERATORS:
            segments.append([])
        else:
            target = next(stream, None)
            if target is None or target[0] != "word" or not _redirect_is_safe(text, target[1]):
                return None
    return segments


def _matches_flag(arg: str, flag: str) -> bool:
    if flag.startswith("--"):
        name = arg[2:].split("=", 1)[0] if arg.startswith("--") else ""
        return bool(name) and flag[2:].startswith(name)
    return arg.startswith("-") and not arg.startswith("--") and flag[1] in arg[1:]


def _segment_is_read_only(words: list[str]) -> bool:
    """True only if one command segment starts with an allowlisted reader and
    passes it nothing that writes or executes.

    Interpreters like ``python -c`` / ``node -e`` / ``awk`` are deliberately
    NOT in the allowlist because they run arbitrary code.
    """
    prefix = next(
        (p for p in (tuple(words[:k]) for k in (3, 2, 1)) if p in _READ_ONLY_COMMANDS), None
    )
    if prefix is None:
        return False
    args = words[len(prefix) :]
    if prefix == ("env",):
        return not args
    if prefix == ("find",):
        return not any(a in _FIND_WRITE_ACTIONS for a in args)
    flags = _WRITE_OR_EXEC_FLAGS.get(prefix, ())
    return not any(_matches_flag(a, f) for a in args for f in flags)


def _is_read_only_command(command: str) -> bool:
    """Check if a shell command is read-only (safe to execute without approval).

    Every segment between ``|``, ``&&``, ``||``, ``;`` and ``&`` must be
    read-only on its own, so neither ``cat script | bash`` nor
    ``echo hi && touch x`` bypasses approval. A leading ``cd DIR`` segment is
    ignored when something follows it.
    """
    tokens = _shell_tokens(command)
    segments = _split_segments(tokens) if tokens is not None else None
    if not segments:
        return False
    if len(segments) > 1 and segments[0][:1] == ["cd"]:
        segments = segments[1:]
    return all(_segment_is_read_only(words) for words in segments)
