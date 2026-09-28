"""
Command validation — defense-in-depth checks for shell commands.

Each validator is a function that takes a command string and returns a
warning string if the command is suspicious, or None if it passes.

validate_command() runs all validators against each subcommand after
splitting compound commands on ;, &&, ||.
"""

import os
import re
import shlex

from server.security.sensitive_paths import validator_path_patterns

# ---------------------------------------------------------------------------
# Subcommand splitting
# ---------------------------------------------------------------------------

# Matches unquoted ;, &&, || as command separators
_SPLIT_PATTERN = re.compile(
    r"""(?:[^"'\\;|&]|"[^"]*"|'[^']*'|\\.)+""",
    re.DOTALL,
)


def split_subcommands(command: str) -> list[str]:
    """Split a compound command into subcommands on ;, &&, ||.

    Respects single and double quotes — does not split inside them.
    Returns a list of stripped, non-empty subcommand strings.
    """
    parts = _SPLIT_PATTERN.findall(command)
    return [p.strip() for p in parts if p.strip()]


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
    re.compile(r"\brm\s+-rf\s+/"),  # rm -rf with absolute path
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
# rm / recursive-delete detection — the "never without a human's approval"
# guardrail. Deliberately separate from _DANGEROUS_PATTERNS: those BLOCK a
# command outright (validate_command's callers reject it, full stop); this
# one only marks a command for the stronger approval rule in
# server/tool_executor.py and server/approval/bootstrap.py (always ask a real
# human, never satisfied by a session-wide "allow all commands", the
# auto-mode classifier, bypassPermissions, or an unattended agent's
# unconditional auto-approve). A command an agent or a blanket approval would
# otherwise run silently must still stop for `rm`.
# ---------------------------------------------------------------------------

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
# is checked too (see _segment_deletes).
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
# everything after them is checked.
_SHELL_KEYWORDS = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "{"})
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


def _runs_deleting_script(tokens: list[str], i: int, depth: int) -> bool:
    """True when ``tokens[i]`` is a shell whose -c script, or ``eval`` whose
    arguments, delete files."""
    base = _base(tokens[i])
    if base == "eval":
        return _is_rm(" ".join(tokens[i + 1 :]), depth + 1)
    if base in _SHELLS:
        script = _shell_script(tokens, i)
        return script is not None and _is_rm(script, depth + 1)
    return False


def _segment_deletes(segment: str, depth: int) -> bool:
    """One pipeline stage: its leading command (past environment-variable
    assignments such as ``FOO=bar rm`` and any invocation wrapper) is a delete
    verb, or a shell/eval running a deleting script. Behind a wrapper (or a
    reserved word such as ``then``) every later word is checked as well, since
    a wrapper option's value can sit where the command's name would be. A segment that cannot be tokenized
    (unbalanced quotes) or names no command at all counts as deleting: a parse
    failure is exactly the kind of thing an obfuscated command produces."""
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        return True

    def _skip_assignments_and_flags(i: int) -> int:
        while i < len(tokens) and (
            re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i]) or tokens[i].startswith("-")
        ):
            i += 1
        return i

    i = _skip_assignments_and_flags(0)
    wrapped = False
    while i < len(tokens) and (
        _base(tokens[i]) in _INVOKE_WRAPPERS or tokens[i] in _SHELL_KEYWORDS
    ):
        wrapped = True
        i = _skip_assignments_and_flags(i + 1)  # the wrapper's own flags/env args
    if i >= len(tokens):
        return True
    last = len(tokens) if wrapped else i + 1
    for j in range(i, last):
        if _base(tokens[j]) in _DELETE_VERBS:
            return True
        if _runs_deleting_script(tokens, j, depth):
            return True
    return False


def _is_rm(command: str, depth: int) -> bool:
    if depth > _MAX_SCRIPT_DEPTH:
        return True
    command = strip_heredoc_bodies(command)
    if _FIND_DELETE_RE.search(command):
        return True
    # split_subcommands only splits on ;/&&/||, not bare newlines — a
    # multi-line script has no separator between lines at all, so without
    # this a line merely following an rm-free first line would never be
    # inspected on its own.
    for line in command.split("\n"):
        for sub in split_subcommands(line):
            for segment in sub.split("|"):
                segment = segment.strip()
                if segment and _segment_deletes(segment, depth):
                    return True
    return False


def is_rm_command(command: str) -> bool:
    """True if any statement in this shell command deletes files: rm and its
    close cousins directly, `sudo`/`env`/`nohup`/`time`/etc.-wrapped, piped,
    chained with ;/&&/||, on its own line with no separator (a multi-line
    terminal_run/workflow script), inside a shell's -c script (`sh -c
    "rm -rf build"`, `bash -lc '...'`) or an `eval`, or find's own -delete /
    -exec rm. A parse failure on any segment (unbalanced quotes, the kind of
    thing a disguised command produces) is treated as rm-like: fail toward
    requiring approval, not away from it.

    Heredoc bodies are stripped first: they are literal data (e.g. a file
    being written), not commands, so text that merely CONTAINS the word "rm"
    must not trip this.
    """
    return _is_rm(command, 0)


# ---------------------------------------------------------------------------
# Command substitution detection
# ---------------------------------------------------------------------------

# Matches $(...), `...`, <(...), >(...)  outside of single quotes
_COMMAND_SUBST_PATTERNS = [
    re.compile(r"\$\("),  # $( ... )
    re.compile(r"`"),  # backtick substitution
    re.compile(r"<\("),  # process substitution <( ... )
    re.compile(r">\("),  # process substitution >( ... )
]


def check_command_substitution(command: str) -> str | None:
    """Detect command substitution patterns that could hide malicious commands."""
    # Strip content inside single quotes (safe — no expansion)
    stripped = re.sub(r"'[^']*'", "", command)
    # Allow heredoc-style input: $(cat <<...) — safe pattern used by
    # git commit -m, gh pr create --body, etc.
    stripped = re.sub(r"\$\(cat\s+<<", "", stripped)
    for pattern in _COMMAND_SUBST_PATTERNS:
        if pattern.search(stripped):
            return f"Blocked: command substitution detected ({pattern.pattern})"
    return None


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


def validate_command(command: str) -> str | None:
    """Validate a command by splitting into subcommands and running all validators.

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

    subcommands = split_subcommands(command)
    for sub in subcommands:
        for validator in _VALIDATORS:
            if validator is check_subcommand_cap:
                continue  # Already checked on the full command
            warning = validator(sub)
            if warning:
                return warning
    return None
