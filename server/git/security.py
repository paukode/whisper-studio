"""Secret content detection for git commits.

The git executor refuses a commit only when the staged changes contain a
real secret. A file NAME never blocks a commit: ``tests/test_secret_reads.py``
or ``docs/password-policy.md`` commit like any other file. A credential file
(``.env`` with real values, a private key) is still refused, because its
CONTENT has the shape of a secret.

The scan reads only the lines a commit adds (``git diff --cached -U0``). It
reuses the provider rules of ``server/memory/secret_scanner.py`` and adds
rules that need more context than one regex: a private key block with a key
body, a password or secret assignment with a high-entropy value, and a
password inside a URL. Placeholders (``AKIA...EXAMPLE``, ``your-token-here``,
``changeme``, alphabet runs) never match. A finding names the file, the line
and the kind of secret, never the value.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from server.memory.secret_scanner import RULES

# The header-only rule of the memory scanner fires on a doc that only names
# a key format. A commit needs the key body too (see _private_key_blocks).
_PROVIDER_RULES = tuple(r for r in RULES if r[0] != "private-key-header")

_KEY_HEADER = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")
_KEY_BODY = re.compile(r"^[A-Za-z0-9+/=]{40,}$")
# Header and body on ONE line: a key held in a JSON string (a cloud
# service-account file) or an escaped literal. "\\n" is the two characters
# a JSON string uses for a newline.
_KEY_INLINE = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----(?:\\n|\s)*([A-Za-z0-9+/=]{40,})"
)
# PEM and PGP armor headers that can sit between the BEGIN line and the body.
_KEY_ARMOR = re.compile(r"^(?:Proc-Type|DEK-Info|Version|Comment|Hash):|^$")

_SECRET_NAME = (
    r"[A-Za-z0-9_.\-]*"
    r"(?:password|passwd|passphrase|pwd|secret|token|api[_\-]?key|apikey"
    r"|access[_\-]?key|private[_\-]?key|credential|auth[_\-]?key)"
    r"[A-Za-z0-9_.\-]*"
)
# name = "value" or name: 'value' (source code, YAML, JSON). A literal that
# a "+" continues is a fragment of a string built at runtime, not a secret.
_QUOTED_ASSIGN = re.compile(
    r"(?i)[\"']?\b" + _SECRET_NAME + r"[\"']?\s*(?::=|=|:)\s*([\"'])([^\"'\s]{12,})\1(?!\s*\+)"
)
# NAME=value on its own line (.env files, shell exports), with an optional
# trailing comment.
_ENV_ASSIGN = re.compile(
    r"(?i)^\s*(?:export\s+)?" + _SECRET_NAME + r"\s*=\s*([^\s\"'#]{12,})\s*(?:#.*)?$"
)
# name: value with no quotes on its own line (YAML: docker-compose, k8s,
# application.yml), also as a list item, with an optional trailing comment.
_YAML_ASSIGN = re.compile(
    r"(?i)^\s*(?:-\s+)?" + _SECRET_NAME + r"\s*:\s+([^\s\"'#]{12,})\s*(?:#.*)?$"
)
# scheme://user:password@host
_URL_PASSWORD = re.compile(r"\b[a-z][a-z0-9+.\-]*://[^/\s:@'\"]+:([^/\s:@'\"]{8,})@")

_PLACEHOLDER_WORDS = (
    "example",
    "placeholder",
    "dummy",
    "fake",
    "sample",
    "changeme",
    "redacted",
    "your",
    "xxxx",
    "****",
    "...",
)
# Words that mark a generic value as made up. A provider key with a fixed
# prefix is random enough that these never sit inside a real one by chance,
# but a hand-typed password often reads like this.
_GENERIC_PLACEHOLDER_WORDS = ("test", "secret", "password", "token", "mock", "invalid")
_SEQUENCES = ("abcdefgh", "01234567", "12345678", "qwerty")
_CODE_CHARS = set("()[]{}$<>%\\`")
_DOTTED_NAME = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")
_WORDY_NAME = re.compile(r"^[A-Za-z]+(?:[_\-.][A-Za-z]+)*[_\-.]?\d{0,4}$")


@dataclass(frozen=True)
class SecretFinding:
    """One secret in the staged changes. It never holds the value."""

    path: str
    line: int
    label: str


def _entropy(value: str) -> float:
    counts: dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(value)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def _is_placeholder(value: str) -> bool:
    low = value.lower()
    if any(word in low for word in _PLACEHOLDER_WORDS):
        return True
    if any(seq in low for seq in _SEQUENCES):
        return True
    if re.search(r"(.)\1{5,}", value):
        return True
    return len(set(value)) < 6


def _looks_random(value: str) -> bool:
    """A generic value (no provider prefix) counts as a secret only when it
    reads like generated text, not like a name, a path or code."""
    if _is_placeholder(value):
        return False
    low = value.lower()
    if any(word in low for word in _GENERIC_PLACEHOLDER_WORDS):
        return False
    if any(ch in _CODE_CHARS for ch in value):
        return False
    if _DOTTED_NAME.match(value) or _WORDY_NAME.match(value):
        return False
    if not (re.search(r"[A-Za-z]", value) and re.search(r"\d", value)):
        return False
    return _entropy(value) >= 3.5


def _line_findings(text: str) -> list[str]:
    """Return the labels of the secrets on one added line."""
    labels: list[str] = []
    for _rule_id, label, pattern in _PROVIDER_RULES:
        # Every match counts: a placeholder must not hide a real key that
        # sits later on the same line.
        if any(not _is_placeholder(m.group(1)) for m in pattern.finditer(text)):
            labels.append(label)
    if labels:
        return labels
    for match in _QUOTED_ASSIGN.finditer(text):
        if _looks_random(match.group(2)):
            return ["Password or secret assignment"]
    for assign in (_ENV_ASSIGN, _YAML_ASSIGN):
        match = assign.match(text)
        if match and _looks_random(match.group(1)):
            return ["Password or secret assignment"]
    match = _URL_PASSWORD.search(text)
    if match and _looks_random(match.group(1)):
        return ["Password in a URL"]
    return []


def _private_key_blocks(lines: list[tuple[int, str]]) -> list[int]:
    """Line numbers of BEGIN headers that a key body follows, on the same
    line or on the next lines."""
    found: list[int] = []
    for i, (lineno, text) in enumerate(lines):
        if not _KEY_HEADER.search(text):
            continue
        inline = _KEY_INLINE.search(text)
        if inline:
            if not _is_placeholder(inline.group(1)):
                found.append(lineno)
            continue
        for _next_no, nxt in lines[i + 1 : i + 6]:
            body = nxt.strip().strip("\"',").replace("\\n", "")
            if _KEY_BODY.match(body):
                found.append(lineno)
                break
            if not _KEY_ARMOR.match(body):
                break
    return found


def scan_added_lines(path: str, lines: list[tuple[int, str]]) -> list[SecretFinding]:
    """Scan the added lines of one file. ``lines`` holds (line number, text)."""
    findings: list[SecretFinding] = [
        SecretFinding(path, lineno, "Private key") for lineno in _private_key_blocks(lines)
    ]
    for lineno, text in lines:
        for label in _line_findings(text):
            findings.append(SecretFinding(path, lineno, label))
    findings.sort(key=lambda f: f.line)
    return findings


def _diff_path(header: str) -> str | None:
    """The new path from a ``+++ b/<path>`` line, None for a deletion."""
    raw = header[4:].rstrip("\n")
    if raw == "/dev/null":
        return None
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
        raw = raw.encode("latin-1", "ignore").decode("utf-8", "replace")
    return raw[2:] if raw.startswith("b/") else raw


def added_lines_by_file(diff_text: str) -> dict[str, list[tuple[int, str]]]:
    """Parse ``git diff -U0`` output into the added lines of each file."""
    files: dict[str, list[tuple[int, str]]] = {}
    path: str | None = None
    in_hunk = False
    lineno = 0
    for line in diff_text.split("\n"):
        if line.startswith("diff --git "):
            path, in_hunk = None, False
            continue
        if not in_hunk:
            if line.startswith("+++ "):
                path = _diff_path(line)
            elif line.startswith("@@ "):
                match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", line)
                in_hunk = bool(match)
                lineno = int(match.group(1)) if match else 0
            continue
        if line.startswith("@@ "):
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", line)
            lineno = int(match.group(1)) if match else lineno
        elif line.startswith("+"):
            if path is not None:
                files.setdefault(path, []).append((lineno, line[1:]))
            lineno += 1
        elif line.startswith(" "):
            lineno += 1
    return files


def scan_diff(diff_text: str) -> list[SecretFinding]:
    """Scan a staged diff and return every secret it adds."""
    findings: list[SecretFinding] = []
    for path, lines in added_lines_by_file(diff_text).items():
        findings.extend(scan_added_lines(path, lines))
    return findings


def format_refusal(findings: list[SecretFinding], limit: int = 20) -> str:
    """The refusal text: file, line and kind of each secret, never the value."""
    rows = [f"  {f.path}:{f.line}  {f.label}" for f in findings[:limit]]
    if len(findings) > limit:
        rows.append(f"  ... and {len(findings) - limit} more")
    return (
        "refusing to commit: the staged changes contain secrets.\n"
        + "\n".join(rows)
        + "\nNothing was staged or committed. Move each value to an environment "
        "variable or a gitignored file, then commit again."
    )
