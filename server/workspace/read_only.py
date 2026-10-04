"""Classifier that decides whether ws_run_command may skip the approval card.

It fails closed: a command runs without approval only when it is provably
read-only. The command is lexed the way /bin/sh (and zsh, which the shell
snapshot may re-exec under) splits it, then every segment between control
operators must start with an allowlisted reader and carry no argument or
redirect that writes a file or launches another program. Shell syntax the
lexer does not model (substitution, subshells, newlines) needs approval.

Some readers write through an operand instead of a flag (`git branch NAME`,
`uniq IN OUT`, a sed `w` command). Those get an operand rule: their options
are parsed against an allowlist, the way git and getopt read them, and the
operands must be the read-only shape.
"""

from collections.abc import Callable
from dataclasses import dataclass
from string import punctuation

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
    ("ag",): ("--pager",),
    ("sort",): ("-o", "--output", "--compress-program"),
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


@dataclass(frozen=True)
class _Options:
    """The options a reader accepts while it stays read-only. Anything else
    fails closed, which covers every writer flag without naming it."""

    short: str = ""  # short flags with no value
    short_valued: str = ""  # value is the rest of the word, else the next word
    short_attached: str = ""  # optional value glued on only (`git tag -n3`)
    long: tuple[str, ...] = ()  # no value, or an optional `=value`
    long_valued: tuple[str, ...] = ()  # value after `=`, else the next word
    # What a dash word after the first operand is: "permute" parses it as an
    # option (git); "subcommand" leaves it to the subcommand (`git remote`);
    # "strict" refuses it, since GNU getopt reads it as an option but BSD
    # getopt as an operand.
    after_operand: str = "permute"


def _resolve_long(name: str, names: tuple[str, ...]) -> str | None:
    """The option ``name`` stands for: an exact match, else the one name it
    abbreviates. git and getopt_long resolve the same way and reject an
    ambiguous prefix, so a name we cannot pin down never runs as a writer."""
    if name in names:
        return name
    matches = [n for n in names if name and n.startswith(name)]
    return matches[0] if len(matches) == 1 else None


def _parse_options(
    args: list[str], spec: _Options
) -> tuple[list[tuple[str, str | None]], list[str]] | None:
    """Split ``args`` into ([(option, value)], operands), or None when an
    option is not in ``spec``. Options come back as `-x` or `--name`."""
    options: list[tuple[str, str | None]] = []
    operands: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            operands.extend(args[i:])
            break
        if arg == "-" or not arg.startswith("-"):
            operands.append(arg)
            if spec.after_operand == "permute":
                continue
            rest = args[i:]
            if spec.after_operand == "strict" and any(a.startswith("-") and a != "-" for a in rest):
                return None
            operands.extend(rest)
            break
        if arg.startswith("--"):
            name, eq, value = arg[2:].partition("=")
            full = _resolve_long(name, spec.long + spec.long_valued)
            if full is None:
                return None
            if not eq:
                value = None
                if full in spec.long_valued and i < len(args):
                    value = args[i]
                    i += 1
            options.append((f"--{full}", value))
            continue
        for j in range(1, len(arg)):
            c, rest = arg[j], arg[j + 1 :]
            if c in spec.short_valued:
                value = rest or None
                if not rest and i < len(args):
                    value = args[i]
                    i += 1
                options.append((f"-{c}", value))
                break
            if c in spec.short_attached:
                options.append((f"-{c}", rest))
                break
            if c not in spec.short:
                return None
            options.append((f"-{c}", None))
    return options, operands


_GIT_REMOTE_OPTIONS = _Options(short="v", long=("verbose",), after_operand="subcommand")


def _git_remote_reads(args: list[str]) -> bool:
    """`git remote [-v]`, `show`, and `get-url` read; every other subcommand
    rewrites the remote config (a set-url aims the next approved push)."""
    parsed = _parse_options(args, _GIT_REMOTE_OPTIONS)
    return parsed is not None and parsed[1][:1] in ([], ["show"], ["get-url"])


# `git branch` and `git tag` list-mode options. The filters take the next
# word as their commit unless it is last, exactly like the valued options.
_GIT_REF_FILTERS = ("contains", "no-contains", "merged", "no-merged", "points-at")
_GIT_REF_DISPLAY = ("ignore-case", "color", "no-color", "column", "no-column", "omit-empty")
_GIT_BRANCH_OPTIONS = _Options(
    short="arvliq",
    long=(
        "all",
        "remotes",
        "verbose",
        "list",
        "show-current",
        "quiet",
        "abbrev",
        "no-abbrev",
        *_GIT_REF_DISPLAY,
    ),
    long_valued=(*_GIT_REF_FILTERS, "sort", "format"),
)
_GIT_TAG_OPTIONS = _Options(
    short="li",
    short_attached="n",
    long=("list", *_GIT_REF_DISPLAY),
    long_valued=(*_GIT_REF_FILTERS, "sort", "format"),
)
# Options that put git in list mode, where operands are patterns. Without one
# an operand is a ref to create (`git branch -v NAME` creates NAME).
_GIT_LIST_MODE = frozenset({"-l", "--list", *(f"--{f}" for f in _GIT_REF_FILTERS)})


def _git_ref_lister(spec: _Options, list_mode: frozenset[str]) -> Callable[[list[str]], bool]:
    def lists(args: list[str]) -> bool:
        parsed = _parse_options(args, spec)
        if parsed is None:
            return False
        options, operands = parsed
        return not operands or any(name in list_mode for name, _ in options)

    return lists


_UNIQ_OPTIONS = _Options(
    short="cdDiuz0123456789",
    short_valued="fsw",
    long=("count", "repeated", "all-repeated", "unique", "ignore-case", "zero-terminated", "group"),
    long_valued=("skip-fields", "skip-chars", "check-chars"),
    after_operand="strict",
)


def _uniq_reads(args: list[str]) -> bool:
    """`uniq IN OUT` writes OUT, so at most one operand."""
    parsed = _parse_options(args, _UNIQ_OPTIONS)
    return parsed is not None and len(parsed[1]) <= 1


_SED_OPTIONS = _Options(
    short="nErsuz",
    short_valued="e",
    long=(
        "quiet",
        "silent",
        "regexp-extended",
        "separate",
        "unbuffered",
        "null-data",
        "zero-terminated",
        "posix",
    ),
    long_valued=("expression",),
    after_operand="strict",
)


def _sed_prints_only(args: list[str]) -> bool:
    """`sed -n` runs only when every script just prints (see
    _sed_script_prints_only). With no -e the first operand is the script."""
    parsed = _parse_options(args, _SED_OPTIONS)
    if parsed is None:
        return False
    options, operands = parsed
    scripts = [v for name, v in options if name in ("-e", "--expression")] or operands[:1]
    return bool(scripts) and all(v is not None and _sed_script_prints_only(v) for v in scripts)


def _sed_script_prints_only(script: str) -> bool:
    """True when each command is an optional address range, an optional `!`,
    then one of p P = l q Q { } or s/re/rep/ with flags from g p i I m M and
    digits. Everything else fails closed: the w W r R e commands, the s///w
    and s///e flags, labels (GNU ends one at `;`, BSD at the newline), and
    any regex GNU and BSD sed would end in different places."""
    i, n = 0, len(script)
    while True:
        while i < n and script[i] in " \t\n;":
            i += 1
        if i == n:
            return True
        end = _sed_address_range(script, i)
        if end is None:
            return False
        i = _skip_blanks(script, end)
        if script[i : i + 1] == "!":
            i = _skip_blanks(script, i + 1)
        command = script[i : i + 1]
        i += 1
        if command == "{":
            continue
        if command == "s":
            end = _sed_substitution_end(script, i)
            if end is None:
                return False
            i = end
        elif command == "l":
            while i < n and script[i].isdigit():
                i += 1
        elif command not in ("p", "P", "=", "q", "Q", "}"):
            return False
        i = _skip_blanks(script, i)
        if i < n and script[i] not in ";\n}":
            return False


def _skip_blanks(script: str, i: int) -> int:
    while i < len(script) and script[i] in " \t":
        i += 1
    return i


def _sed_address_range(script: str, i: int) -> int | None:
    """Index past an optional `addr1[,addr2]`, or None if malformed."""
    end = _sed_address(script, i)
    if end is None or end == i:
        return end
    after = _skip_blanks(script, end)
    if script[after : after + 1] != ",":
        return end
    start = _skip_blanks(script, after + 1)
    if script[start : start + 1] in ("+", "~"):  # GNU addr1,+N and addr1,~N
        start += 1
        end = _sed_digits_end(script, start)
    else:
        end = _sed_address(script, start)
    return None if end is None or end == start else end


def _sed_address(script: str, i: int) -> int | None:
    """Index past one address (N, first~step, $, /re/, \\cREc), i when there
    is none, or None if malformed."""
    c = script[i : i + 1]
    if c.isdigit():
        end = _sed_digits_end(script, i)
        if script[end : end + 1] == "~":
            step = _sed_digits_end(script, end + 1)
            return step if step > end + 1 else None
        return end
    if c == "$":
        return i + 1
    if c == "/":
        end = _sed_regex_end(script, i + 1, "/")
    elif c == "\\" and _sed_delimiter_ok(script[i + 1 : i + 2]):
        end = _sed_regex_end(script, i + 2, script[i + 1])
    elif c == "\\":
        return None
    else:
        return i
    while end is not None and script[end : end + 1] in ("I", "M"):
        end += 1
    return end


def _sed_digits_end(script: str, i: int) -> int:
    while i < len(script) and script[i].isdigit():
        i += 1
    return i


def _sed_delimiter_ok(c: str) -> bool:
    return len(c) == 1 and c in punctuation and c not in "\\[]"


def _sed_substitution_end(script: str, i: int) -> int | None:
    """Index past `s` arguments starting at the delimiter: regex, replacement,
    then only the print-safe flags."""
    delim = script[i : i + 1]
    if not _sed_delimiter_ok(delim):
        return None
    end = _sed_regex_end(script, i + 1, delim)
    end = _sed_text_end(script, end, delim) if end is not None else None
    while end is not None and end < len(script) and script[end] in "gpiImM0123456789":
        end += 1
    return end


def _sed_text_end(script: str, i: int, delim: str) -> int | None:
    """Index past the delimiter that ends a regex the GNU way, or a
    replacement either way: a backslash escapes the next character. None when
    it does not end, or crosses a newline the two seds could join."""
    while i < len(script):
        c = script[i]
        if c == "\n" or (c == "\\" and script[i + 1 : i + 2] in ("", "\n")):
            return None
        if c == delim:
            return i + 1
        i += 2 if c == "\\" else 1
    return None


def _sed_regex_end(script: str, i: int, delim: str) -> int | None:
    """Where a regex ends, only if GNU and BSD sed agree. BSD skips a
    delimiter inside a bracket expression (`/[/]/`) and GNU does not, so a
    script that hides a command there reads differently to each."""
    gnu = _sed_text_end(script, i, delim)
    return gnu if gnu is not None and gnu == _bsd_regex_end(script, i, delim) else None


def _bsd_regex_end(script: str, i: int, delim: str) -> int | None:
    """FreeBSD sed compile_delimited: brackets hold the delimiter literally."""
    n = len(script)
    while i < n:
        c = script[i]
        if c == "[":
            i = _bsd_bracket_end(script, i)
            if i is None:
                return None
            continue
        if c == "\\" and script[i + 1 : i + 2] in ("[", delim, "n", "\\"):
            i += 2
            continue
        if c == delim:
            return i + 1
        i += 1
    return None


def _bsd_bracket_end(script: str, i: int) -> int | None:
    """FreeBSD sed compile_ccl: index past the `]` that closes the bracket
    expression opening at i, with [:class:], [=equiv=] and [.coll.] inside."""
    n = len(script)
    i += 1
    if script[i : i + 1] == "^":
        i += 1
    if script[i : i + 1] == "]":
        i += 1
    while i < n and script[i] != "]":
        if script[i] == "[" and script[i + 1 : i + 2] in (".", ":", "="):
            kind = script[i + 1]
            i += 3
            while i < n and not (script[i] == "]" and script[i - 1] == kind):
                i += 1
            if i >= n:
                return None
        i += 1
    return i + 1 if i < n else None


# Readers that write or launch through an operand rather than a flag.
_OPERAND_RULES: dict[tuple[str, ...], Callable[[list[str]], bool]] = {
    ("git", "remote"): _git_remote_reads,
    ("git", "branch"): _git_ref_lister(_GIT_BRANCH_OPTIONS, _GIT_LIST_MODE),
    ("git", "tag"): _git_ref_lister(_GIT_TAG_OPTIONS, _GIT_LIST_MODE | {"-n"}),
    ("uniq",): _uniq_reads,
    ("sed", "-n"): _sed_prints_only,
}


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
    operand_rule = _OPERAND_RULES.get(prefix)
    if operand_rule is not None and not operand_rule(args):
        return False
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
