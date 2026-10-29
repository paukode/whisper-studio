"""
Command validation — defense-in-depth checks for shell commands.

Each validator is a function that takes a command string and returns a
warning string if the command is suspicious, or None if it passes.

validate_command() runs all validators against each subcommand after
splitting compound commands on ;, &&, ||.
"""

import os
import re

from server.security.sensitive_paths import validator_path_patterns

# ---------------------------------------------------------------------------
# Subcommand splitting
# ---------------------------------------------------------------------------

# Matches unquoted ;, &&, || as command separators. The & of a redirection
# (`2>&1`, `<&3`, `&>/dev/null`) belongs to its word: splitting there left
# `1 rm -rf x` with `1` read as the command.
_SPLIT_PATTERN = re.compile(
    r"""(?:[<>]&|&>|[^"'\\;|&]|"[^"]*"|'[^']*'|\\.)+""",
    re.DOTALL,
)


def split_subcommands(command: str) -> list[str]:
    """Split a compound command into subcommands on ;, &&, || and the pipes
    and background & between them (every unquoted ;, & and |).

    Respects single and double quotes: does not split inside them.
    Returns a list of stripped, non-empty subcommand strings.
    """
    parts = _SPLIT_PATTERN.findall(command)
    return [p.strip() for p in parts if p.strip()]


def split_statements(line: str) -> list[tuple[str, str, str]]:
    """split_subcommands of one line, each with the operators around it: the
    run of ;, & and | before it and the one after it ("" at either end of the
    line)."""
    found = [m for m in _SPLIT_PATTERN.finditer(line) if m.group().strip()]
    statements = []
    for k, m in enumerate(found):
        before = line[found[k - 1].end() if k else 0 : m.start()].strip()
        after = line[m.end() : found[k + 1].start() if k + 1 < len(found) else len(line)]
        statements.append((before, m.group().strip(), after.strip()))
    return statements


# Heredoc opener: <<DELIM, <<-DELIM, <<'DELIM', <<"DELIM"
_HEREDOC_OPEN = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def strip_heredoc_bodies(command: str) -> str:
    """Remove heredoc bodies before validation.

    A heredoc body (`cat > f <<'EOF' … EOF`) is LITERAL DATA, not shell — so
    its contents must not be scanned for command separators or dangerous
    patterns. Without this, writing a markdown file with a table (full of `|`
    pipes) tripped the chained-command cap, since every `|` looked like a
    pipeline. Keeps the opener line; drops the body and the closing delimiter.
    """
    lines = command.split("\n")
    out: list[str] = []
    delim: str | None = None
    for line in lines:
        if delim is None:
            out.append(line)
            m = _HEREDOC_OPEN.search(line)
            if m:
                delim = m.group(2)
        elif line.strip() == delim:
            delim = None  # closing line — also literal, drop it
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Dangerous pattern validator
# ---------------------------------------------------------------------------

_DANGEROUS_PATTERNS = [
    re.compile(r";\s*rm\s"),  # rm after semicolon
    re.compile(r">\s*/etc/"),  # redirect to system dirs
    re.compile(r"/proc/.*/environ"),  # proc environ access
    # rm -rf of the filesystem root itself. Any other absolute path is the rm
    # floor's to judge (server.security.delete_scope): inside the workspace or
    # the app's folders the mode decides, anywhere else a person approves. A
    # block on every absolute path refused the session's own scratch cleanup
    # and pushed the model to spell the path as $VAR, which always asks.
    re.compile(r"\brm\s+-rf\s+/+\*?(?:\s|$)"),
    re.compile(r"mkfs\b"),  # format filesystem
    re.compile(r"dd\s+.*of=/dev/"),  # dd to device
]


def check_dangerous_patterns(command: str) -> str | None:
    """Check for known dangerous command patterns."""
    for pattern in _DANGEROUS_PATTERNS:
        if pattern.search(command):
            return f"Blocked: dangerous pattern ({pattern.pattern})"
    return None


# ---------------------------------------------------------------------------
# Command substitution detection
# ---------------------------------------------------------------------------

# Matches $(...), `...`, <(...), >(...)  outside of single quotes. A closed
# $( ... ) never reaches it: _vet_substitutions checks it first and leaves a
# plain word in its place.
_COMMAND_SUBST_PATTERNS = [
    re.compile(r"\$\("),  # $( ... )
    re.compile(r"`"),  # backtick substitution
    re.compile(r"<\("),  # process substitution <( ... )
    re.compile(r">\("),  # process substitution >( ... )
]


def check_command_substitution(command: str) -> str | None:
    """Detect command substitution patterns that could hide malicious commands."""
    # Strip content inside single quotes (safe, no expansion)
    stripped = re.sub(r"'[^']*'", "", command)
    for pattern in _COMMAND_SUBST_PATTERNS:
        if pattern.search(stripped):
            return f"Blocked: command substitution detected ({pattern.pattern})"
    return None


# What makes a substitution's output run as a command: words that may come
# before a statement's program name, and programs that run their arguments.
_STATEMENT_LEAD = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "{"})
_RUNS_ITS_ARGUMENTS = frozenset({"eval", "source", "."})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_SHELL_C_FLAG = re.compile(r"-[a-z]*c[a-z]*")
# git commit -m "$(cat <<'EOF' ... EOF)": the heredoc body is already gone.
_HEREDOC_TEXT = re.compile(r"\s*cat\s+<<")
_SUBST_WORD = "SUBST"
_SUBST_SHOWN = 80


def _closing_paren(command: str, j: int) -> int | None:
    """Index of the ``)`` that closes the ``$(`` whose text starts at ``j``."""
    depth, quote = 1, ""
    while j < len(command):
        c = command[j]
        if quote == "'":
            if c == "'":
                quote = ""
        elif c == "\\":
            j += 1
        elif c == "'" and not quote:
            quote = "'"
        elif c == '"':
            quote = "" if quote else '"'
        elif not quote and c == "(":
            depth += 1
        elif not quote and c == ")":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return None


def _substitutions(command: str) -> list[tuple[int, int, int]] | None:
    """(start, end, statement start) of each outermost ``$( ... )`` outside
    single quotes, or None when one is not closed."""
    found = []
    i, stmt, quote = 0, 0, ""
    while i < len(command):
        c = command[i]
        if quote == "'":
            if c == "'":
                quote = ""
        elif c == "\\":
            i += 1
        elif c == "'" and not quote:
            quote = "'"
        elif c == '"':
            quote = "" if quote else '"'
        elif command.startswith("$(", i) and not command.startswith("$((", i):
            end = _closing_paren(command, i + 2)
            if end is None:
                return None
            found.append((i, end + 1, stmt))
            i = end + 1
            continue
        elif not quote and c in ";&|\n(":
            stmt = i + 1
        i += 1
    return found


def _runs_as_command(before: str) -> bool:
    """Whether a substitution that follows ``before`` (its statement up to the
    ``$(``) puts its output where the shell runs it: the program name, or the
    argument of eval, source, or ``sh -c``."""
    words = before.split()
    if before and not before[-1].isspace():
        # Part of a word that started earlier: x$(...), "$(...)", NAME=$(...).
        if _ENV_ASSIGN.match(words.pop()):
            return False
    wrapped = False
    while words:
        word = words[0]
        if word in _CMD_WRAPPERS or word == "xargs":
            wrapped = True
        elif not (
            word in _STATEMENT_LEAD or _ENV_ASSIGN.match(word) or (wrapped and word.startswith("-"))
        ):
            break
        words.pop(0)
    if not words:
        return True
    program = words[0].rsplit("/", 1)[-1]
    if program in _RUNS_ITS_ARGUMENTS:
        return True
    return program in _SHELLS and any(_SHELL_C_FLAG.fullmatch(w) for w in words[1:])


def _vet_substitutions(command: str, secret_reads: bool) -> tuple[str, str | None]:
    """Check each ``$( ... )`` in ``command`` and put a plain word in its place.

    A substitution may run a read-only command, as in the branch check
    ``test "$(git branch --show-current)" = main && git commit ...``: its text
    passes every check here and the read-only classifier. Its output must not
    be run as a command, the way ``$(echo cm0= | base64 -d) -rf x`` builds
    ``rm``. A substitution that is not closed stays in the text, and
    check_command_substitution blocks it.

    Returns the command with each checked substitution replaced, and the first
    warning (None when every substitution passes).
    """
    spans = _substitutions(command)
    if not spans:
        return command, None
    # Lazy: server.workspace pulls fastapi (see _github_tools_in_catalog).
    from server.workspace.read_only import _is_read_only_command

    parts, last = [], 0
    for start, end, stmt in spans:
        inner = command[start + 2 : end - 1]
        shown = command[start:end]
        if len(shown) > _SUBST_SHOWN:
            shown = shown[: _SUBST_SHOWN - 4] + "...)"
        if _runs_as_command(command[stmt:start]):
            return command, (
                f"Blocked: the output of {shown} would run as a command. "
                "Write the command out instead."
            )
        if not _HEREDOC_TEXT.match(inner):
            warning = validate_command(inner, secret_reads=secret_reads)
            if warning:
                return command, f"{warning} (inside {shown})"
            if not _is_read_only_command(inner.strip()):
                return command, (
                    f"Blocked: {shown} runs a command that is not read-only. Only a "
                    "read-only command may run inside $(...); run that one on its own."
                )
        parts += [command[last:start], _SUBST_WORD]
        last = end
    parts.append(command[last:])
    return "".join(parts), None


# ---------------------------------------------------------------------------
# Environment variable injection
# ---------------------------------------------------------------------------

_DANGEROUS_ENV_VARS = re.compile(
    r"(?:^|\s)(?:"
    r"\$BASH_ENV|"
    r"\$ENV|"
    r"\$CDPATH|"
    r"\$IFS|"
    r"\$PROMPT_COMMAND|"
    r"\$LD_PRELOAD|"
    r"\$LD_LIBRARY_PATH|"
    r"BASH_ENV=|"
    r"ENV=|"
    r"CDPATH=|"
    r"IFS=|"
    r"PROMPT_COMMAND=|"
    r"LD_PRELOAD=|"
    r"LD_LIBRARY_PATH="
    r")"
)


def check_env_injection(command: str) -> str | None:
    """Detect dangerous environment variable references or assignments."""
    # Strip content inside single quotes
    stripped = re.sub(r"'[^']*'", "", command)
    match = _DANGEROUS_ENV_VARS.search(stripped)
    if match:
        return f"Blocked: dangerous environment variable ({match.group().strip()})"
    return None


# ---------------------------------------------------------------------------
# Control character and unicode whitespace detection
# ---------------------------------------------------------------------------

# ASCII control characters (except tab \x09, newline \x0a, carriage return \x0d)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Dangerous unicode whitespace that can hide command content
_UNICODE_WHITESPACE = re.compile(
    r"[\u00a0"  # NBSP
    r"\u2000-\u200b"  # en/em space, thin space, etc.
    r"\u200c-\u200f"  # zero-width non-joiner, joiner, LTR/RTL marks
    r"\u2028"  # line separator
    r"\u2029"  # paragraph separator
    r"\u202f"  # narrow NBSP
    r"\u205f"  # medium mathematical space
    r"\u2060"  # word joiner
    r"\u3000"  # ideographic space
    r"\ufeff"  # zero-width NBSP (BOM)
    r"\ufffe"  # noncharacter
    r"]"
)


def check_control_characters(command: str) -> str | None:
    """Detect ASCII control characters and dangerous unicode whitespace."""
    match = _CONTROL_CHARS.search(command)
    if match:
        return f"Blocked: control character (\\x{ord(match.group()):02x})"
    match = _UNICODE_WHITESPACE.search(command)
    if match:
        return f"Blocked: suspicious unicode whitespace (U+{ord(match.group()):04X})"
    return None


# ---------------------------------------------------------------------------
# Obfuscated flag detection
# ---------------------------------------------------------------------------

# Backslash-escaped characters that could mask command names or flags
_BACKSLASH_IN_WORD = re.compile(r"(?<!\s)\\[a-zA-Z0-9]")

# Hex/octal escape sequences in command position
_HEX_OCTAL_ESCAPE = re.compile(r"\\x[0-9a-fA-F]{2}|\\[0-7]{1,3}")

# $'\xNN' ANSI-C quoting used to smuggle characters
_ANSI_C_QUOTING = re.compile(r"\$'[^']*\\x[0-9a-fA-F]")


def check_obfuscated_flags(command: str) -> str | None:
    """Detect obfuscation techniques that mask command names or flags."""
    # Strip content inside single quotes (safe)
    stripped = re.sub(r"'[^']*'", "", command)
    if _HEX_OCTAL_ESCAPE.search(stripped):
        return "Blocked: hex/octal escape sequence in command"
    if _ANSI_C_QUOTING.search(command):
        return "Blocked: ANSI-C quoting with escape sequence"
    if _BACKSLASH_IN_WORD.search(stripped):
        return "Blocked: backslash-escaped character in word"
    return None


# ---------------------------------------------------------------------------
# Sensitive path and file type detection
# ---------------------------------------------------------------------------

# Canonical list lives in server/security/sensitive_paths.py so the
# command validator and the OS sandbox cannot drift on the shared core.
_SENSITIVE_PATHS = validator_path_patterns()

# File extensions that typically contain secrets, keys, or certificates
_SENSITIVE_FILE_EXTENSIONS = re.compile(
    r"\S+\.(?:"
    r"pem|key|p12|pfx|jks|keystore|"  # Private keys / keystores
    r"id_rsa|id_ed25519|id_ecdsa|id_dsa|"  # SSH private keys
    r"gpg|pgp|asc|"  # GPG/PGP keys
    r"kdbx|kwallet|"  # Password manager databases
    r"ovpn"  # VPN config (may contain certs)
    r")(?:\s|$)"
)

# Sensitive filenames (not extension-based)
_SENSITIVE_FILENAMES = re.compile(
    r"(?:^|\s|/)(?:"
    r"\.env\.local|\.env\.production|\.env\.secret|"  # Environment files
    r"\.htpasswd|"  # HTTP auth
    r"\.pgpass|"  # PostgreSQL passwords
    r"credentials\.json|"  # GCP/generic credentials
    r"service[-_]?account.*\.json"  # Service account keys
    r")(?:\s|$)"
)


def check_sensitive_paths(command: str) -> str | None:
    """Detect access to sensitive files, directories, and credential file types.

    Unlike the expansion/substitution validators, this matcher removes quote
    CHARACTERS but PRESERVES their contents. Single/double quotes do not stop
    the shell from reading a file, so ``cat '/etc/shadow'`` and
    ``cat "/Users/me/.ssh/id_rsa"`` must still be caught. Stripping whole
    quoted segments (as the other validators do for expansion safety) would
    delete the path text and let these through.
    """
    # Drop only the quote characters, keeping the path they wrapped.
    stripped = command.replace("'", "").replace('"', "")
    for pattern in _SENSITIVE_PATHS:
        if pattern.search(stripped):
            return f"Blocked: access to sensitive path ({pattern.pattern})"
    match = _SENSITIVE_FILE_EXTENSIONS.search(stripped)
    if match:
        return f"Blocked: access to sensitive file type ({match.group().strip()})"
    match = _SENSITIVE_FILENAMES.search(stripped)
    if match:
        return f"Blocked: access to sensitive file ({match.group().strip()})"
    return None


# ---------------------------------------------------------------------------
# sed -i detection
# ---------------------------------------------------------------------------

# Match sed invocations with the in-place flag in any of its forms:
#   -i             (no suffix)
#   -i.bak / -i'X' (suffix attached, no space)
#   --in-place     / --inplace
# The `(?:\S+\s+)*` lets other tokens (e.g. -e, -E, -n) appear before -i.
_SED_INPLACE = re.compile(r"\bsed\s+(?:\S+\s+)*(?:-i|--in-?place\b)")


def check_sed_inplace(command: str) -> str | None:
    """Detect sed -i (in-place edit) which modifies files directly."""
    if _SED_INPLACE.search(command):
        return "Blocked: sed -i modifies files in-place. Use ws_write_file for file edits."
    return None


# ---------------------------------------------------------------------------
# Subcommand cap — prevents CPU starvation from deeply nested pipelines
# ---------------------------------------------------------------------------

MAX_SUBCOMMANDS = 50


def check_subcommand_cap(command: str) -> str | None:
    """Reject commands with more than MAX_SUBCOMMANDS chained subcommands.

    Counts segments separated by ;, &&, ||, and | (pipes).
    This prevents DoS via CPU starvation from deeply nested command chains.
    """
    # Count all separators (pipes, semicolons, &&, ||) outside quotes
    # Use a simple approach: split on unquoted separators
    stripped = re.sub(r'"[^"]*"', "", command)
    stripped = re.sub(r"'[^']*'", "", stripped)
    # Count pipe segments and command separators
    count = 1  # At least one command
    count += len(re.findall(r"(?:\|(?!\|)|\|\||&&|;)", stripped))
    if count > MAX_SUBCOMMANDS:
        return f"Blocked: too many chained commands ({count} > {MAX_SUBCOMMANDS}). Simplify the command."
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Ordered list of all validators. Each takes a command string, returns
# a warning string or None.
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_CMD_WRAPPERS = frozenset({"env", "command", "sudo", "nohup", "time", "exec", "builtin"})


def _github_tools_in_catalog() -> bool:
    """Whether the authenticated GitHub tools are actually reachable this turn.

    Mirrors the catalog gate in server/chat/tool_pool.py: github / github_api /
    github_api_write only enter the pool when a workspace is connected AND it
    is a git repo. They are deferred tools, so when that gate is closed
    tool_search cannot recover them either: it searches the same catalog they
    were excluded from."""
    try:
        # Lazy: server.workspace pulls fastapi, and this module is imported
        # from places that must stay cheap to import.
        from server.workspace.state import get_workspace_path

        ws = get_workspace_path()
        return bool(ws) and os.path.exists(os.path.join(ws, ".git"))
    except Exception:  # noqa: BLE001 — advice quality only, never block on this
        return False


def check_gh_command(command: str) -> str | None:
    """Block the GitHub CLI in the command sandbox.

    The sandbox has no GitHub auth (the credential file is denied, and any env
    token is stripped), so `gh` there fails silently or misleads — this is the
    exact path behind the false 'closed the PR' incident. The authenticated
    github / github_api / github_api_write tools are the only correct path.

    When those tools are NOT in the catalog, naming them is a dead end: the
    model retries `gh` through the shell, exhausts itself, and hands the user
    a link to click. Say what would actually unblock it instead."""
    toks = command.strip().split()
    i = 0
    while i < len(toks) and (_ENV_ASSIGN.match(toks[i]) or toks[i] in _CMD_WRAPPERS):
        i += 1
    if i < len(toks):
        prog = toks[i].rsplit("/", 1)[-1]
        if prog == "gh":
            if _github_tools_in_catalog():
                return (
                    "`gh` is unavailable in the command sandbox (no GitHub auth). Use the "
                    "github, github_api, or github_api_write tools — they run gh authenticated."
                )
            return (
                "`gh` is unavailable in the command sandbox (no GitHub auth), and the "
                "authenticated github tools are not in this session's tool pool: they "
                "require a connected workspace that is a git repository. Do not retry "
                "`gh` through the shell. Ask the user to connect the repository as the "
                "workspace, then use the github / github_api / github_api_write tools."
            )
    return None


_VALIDATORS: list[callable] = [
    check_subcommand_cap,
    check_dangerous_patterns,
    check_command_substitution,
    check_env_injection,
    check_control_characters,
    check_obfuscated_flags,
    check_sensitive_paths,
    check_sed_inplace,
    check_gh_command,
]


def validate_command(command: str, secret_reads: bool = False) -> str | None:
    """Validate a command by splitting into subcommands and running all validators.

    ``secret_reads`` (a run allowed to read the credential stores, see
    server/security/secret_reads.py) skips the sensitive-path check.

    Returns the first warning found, or None if the command passes all checks.
    """
    # Heredoc bodies are literal data (a written file's contents), not shell.
    # Strip them first so document text — markdown tables full of `|`, etc. —
    # isn't mistaken for command chaining or dangerous patterns.
    command = strip_heredoc_bodies(command)

    # Check the cap on the full command first (before splitting)
    cap_warning = check_subcommand_cap(command)
    if cap_warning:
        return cap_warning

    # Before splitting: a pipe or ; inside $( ... ) is not a separator.
    command, warning = _vet_substitutions(command, secret_reads)
    if warning:
        return warning

    subcommands = split_subcommands(command)
    for sub in subcommands:
        for validator in _VALIDATORS:
            if validator is check_subcommand_cap:
                continue  # Already checked on the full command
            if secret_reads and validator is check_sensitive_paths:
                continue
            warning = validator(sub)
            if warning:
                return warning
    return None
