"""
Lightweight .git/config parser — no subprocess, no external dependencies.

Handles section headers with subsections ([remote "origin"]),
case-insensitive section/key matching, case-sensitive subsections,
quoted values with escape sequences, and inline comments.
"""

import os


def parse_git_config_value(
    git_dir: str,
    section: str,
    subsection: str | None,
    key: str,
) -> str | None:
    """Read a single config value from .git/config.

    Args:
        git_dir: Path to the .git directory
        section: Section name (e.g. "remote") — matched case-insensitively
        subsection: Subsection name (e.g. "origin") — matched case-sensitively, or None
        key: Key name (e.g. "url") — matched case-insensitively

    Returns:
        The config value as a string, or None if not found
    """
    try:
        with open(os.path.join(git_dir, "config")) as f:
            config = f.read()
        return parse_config_string(config, section, subsection, key)
    except OSError:
        return None


def parse_config_string(
    config: str,
    section: str,
    subsection: str | None,
    key: str,
) -> str | None:
    """Parse config value from in-memory config string.

    Exported for testing. Finds first matching key under given section/subsection.
    """
    lines = config.split("\n")
    section_lower = section.lower()
    key_lower = key.lower()

    in_section = False
    for line in lines:
        trimmed = line.strip()

        # Skip empty lines and comment-only lines
        if not trimmed or trimmed[0] == "#" or trimmed[0] == ";":
            continue

        # Section header
        if trimmed[0] == "[":
            in_section = _matches_section_header(trimmed, section_lower, subsection)
            continue

        if not in_section:
            continue

        # Key-value line
        parsed = _parse_key_value(trimmed)
        if parsed and parsed[0].lower() == key_lower:
            return parsed[1]

    return None


def _matches_section_header(
    line: str,
    section_lower: str,
    subsection: str | None,
) -> bool:
    """Check if config line like [remote "origin"] matches given section/subsection."""
    # line starts with '['
    i = 1

    # Read section name
    while i < len(line) and line[i] not in ("]", " ", "\t", '"'):
        i += 1
    found_section = line[1:i].lower()

    if found_section != section_lower:
        return False

    if subsection is None:
        # Simple section: must end with ']'
        return i < len(line) and line[i] == "]"

    # Skip whitespace before subsection quote
    while i < len(line) and line[i] in (" ", "\t"):
        i += 1

    # Must have opening quote
    if i >= len(line) or line[i] != '"':
        return False
    i += 1  # skip opening quote

    # Read subsection — case-sensitive, handle \\ and \" escapes
    found_subsection = ""
    while i < len(line) and line[i] != '"':
        if line[i] == "\\" and i + 1 < len(line):
            next_ch = line[i + 1]
            if next_ch in ("\\", '"'):
                found_subsection += next_ch
                i += 2
                continue
            # Git drops the backslash for other escapes in subsections
            found_subsection += next_ch
            i += 2
            continue
        found_subsection += line[i]
        i += 1

    # Must have closing quote followed by ']'
    if i >= len(line) or line[i] != '"':
        return False
    i += 1  # skip closing quote

    if i >= len(line) or line[i] != "]":
        return False

    return found_subsection == subsection


def _parse_key_value(line: str) -> tuple[str, str] | None:
    """Parse 'key = value' line. Returns (key, value) or None if invalid."""
    # Read key: alphanumeric + hyphen, starting with alpha
    i = 0
    while i < len(line) and _is_key_char(line[i]):
        i += 1
    if i == 0:
        return None
    key = line[:i]

    # Skip whitespace
    while i < len(line) and line[i] in (" ", "\t"):
        i += 1

    # Must have '='
    if i >= len(line) or line[i] != "=":
        # Boolean key with no value — not relevant for our use cases
        return None
    i += 1  # skip '='

    # Skip whitespace after '='
    while i < len(line) and line[i] in (" ", "\t"):
        i += 1

    value = _parse_value(line, i)
    return (key, value)


def _parse_value(line: str, start: int) -> str:
    """Parse config value starting at position. Handles quotes, escapes, inline comments."""
    result = ""
    in_quote = False
    i = start

    while i < len(line):
        ch = line[i]

        # Inline comments outside quotes end the value
        if not in_quote and ch in ("#", ";"):
            break

        if ch == '"':
            in_quote = not in_quote
            i += 1
            continue

        if ch == "\\" and i + 1 < len(line):
            next_ch = line[i + 1]
            if in_quote:
                # Inside quotes: recognize escape sequences
                escape_map = {
                    "n": "\n",
                    "t": "\t",
                    "b": "\b",
                    '"': '"',
                    "\\": "\\",
                }
                if next_ch in escape_map:
                    result += escape_map[next_ch]
                else:
                    # Git silently drops the backslash for unknown escapes
                    result += next_ch
                i += 2
                continue
            # Outside quotes: handle \\
            if next_ch == "\\":
                result += "\\"
                i += 2
                continue
            # Fallthrough — treat backslash literally outside quotes

        result += ch
        i += 1

    # Trim trailing whitespace from unquoted portions.
    if not in_quote:
        result = result.rstrip(" \t")

    return result


def _is_key_char(ch: str) -> bool:
    """Check if character is valid in a git config key (alphanumeric + hyphen)."""
    return ch.isalnum() or ch == "-"
