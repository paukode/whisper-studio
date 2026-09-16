"""Verification evidence for the completion gate: what did the agent PROVE?

Passive and deterministic. It never runs a suite itself. It reads the current
turn's messages and answers: which code files were edited, which test, lint,
typecheck or build commands ran afterwards, and did they pass. When the model
stops right after editing code with no fresh passing evidence, the gate turns
that into a bounded nudge (``verify_on_stop_feedback``), so "done, tests
should pass" is replaced by a real run or an honest blocker. Prose-only edits
(markdown, text, data files) never trigger it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from server.goals.deliverables import turn_messages

MAX_VERIFY_NUDGES = 2
VERIFY_MARKER = "[verify]"

_EDIT_TOOLS = frozenset({"ws_write_file", "ws_edit_file", "ws_create_file"})
_COMMAND_TOOLS = {"ws_run_command": "command", "terminal_run": "command", "run_python": "code"}

_NON_CODE_EXTS = frozenset(
    {
        ".md",
        ".markdown",
        ".mdx",
        ".rst",
        ".txt",
        ".text",
        ".adoc",
        ".org",
        ".log",
        ".csv",
        ".tsv",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".html",
        ".htm",
        ".css",
        ".svg",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".pdf",
        ".docx",
        ".xlsx",
        ".pptx",
    }
)
_NON_CODE_NAMES = frozenset({"license", "licence", "notice", "authors", "changelog", "codeowners"})

# Ordered: the first matching group names the kind. Regexes run over the
# whole command line, so "cd x && python -m pytest -q" still classifies.
_VERIFY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "lint",
        re.compile(
            r"\b(?:ruff (?:check|format)|eslint|flake8|pylint|prettier --check|npm run lint|black --check|golangci-lint|cargo clippy)\b"
        ),
    ),
    ("typecheck", re.compile(r"\b(?:tsc\b|mypy|pyright|npm run typecheck|npm run type-check)")),
    (
        "build",
        re.compile(
            r"\b(?:npm run build|yarn build|pnpm build|vite build|cargo build|go build|make build|mvn (?:compile|package)|gradle build|swift build|xcodebuild)\b"
        ),
    ),
    (
        "test",
        re.compile(
            r"\b(?:pytest|python3? -m pytest|npm test|npm run test|yarn test|pnpm test|vitest|jest|cargo test|go test|make test|make check|mvn test|gradle test|phpunit|rspec|bundle exec rspec|swift test|dotnet test|unittest)\b"
        ),
    ),
)

_FAIL_PATTERNS = (
    re.compile(r"^\[Tool Error\]"),
    re.compile(r"^Error:"),
    re.compile(r"\(exit code [1-9]\d*"),
    re.compile(r"^exit_code: [1-9]\d*", re.MULTILINE),
    re.compile(r"\b\d+ failed\b"),
    re.compile(r"\bFAILED\b"),
    re.compile(r"\berror TS\d+\b"),
    re.compile(r"\bTraceback \(most recent call last\)"),
    re.compile(r"\bVERIFY FAIL\b"),
)


@dataclass
class VerifyRun:
    kind: str
    command: str
    passed: bool
    fresh: bool  # ran after the last code edit of the turn
    pos: int = 0  # message index (within the turn) of the tool_use that ran it


@dataclass
class Evidence:
    edited_paths: list[str] = field(default_factory=list)
    runs: list[VerifyRun] = field(default_factory=list)
    verify_change_pass: bool = False  # a verify_change result said VERIFY PASS after the last edit

    @property
    def fresh_pass(self) -> bool:
        return self.verify_change_pass or any(r.fresh and r.passed for r in self.runs)

    @property
    def fresh_failures(self) -> list[VerifyRun]:
        return [r for r in self.runs if r.fresh and not r.passed]


def is_code_path(path: str) -> bool:
    name = os.path.basename(str(path or "")).lower()
    if not name:
        return False
    root, ext = os.path.splitext(name)
    if ext in _NON_CODE_EXTS:
        return False
    if not ext and root in _NON_CODE_NAMES:
        return False
    return True


def classify_verify_command(command: str) -> str | None:
    """'test' | 'lint' | 'typecheck' | 'build' for a verification command, else None."""
    cmd = (command or "").strip()
    if not cmd:
        return None
    for kind, rx in _VERIFY_PATTERNS:
        if rx.search(cmd):
            return kind
    return None


def result_failed(text: str) -> bool:
    t = text if isinstance(text, str) else str(text)
    return any(rx.search(t) for rx in _FAIL_PATTERNS)


def _result_text(block: dict) -> str:
    raw = block.get("content", block.get("output", ""))
    if isinstance(raw, list):
        raw = " ".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in raw)
    return str(raw or "")


def _tool_input(block: dict) -> dict:
    inp = block.get("input") or block.get("arguments") or {}
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except (TypeError, ValueError):
            inp = {}
    return inp if isinstance(inp, dict) else {}


def turn_evidence(messages: list) -> Evidence:
    """Scan the current turn (since the last real user prompt) for code edits
    and verification runs, pairing each command tool_use with its result.

    Freshness is decided once the whole turn is known: a run counts only when
    it started after the LAST code edit of the turn, so a green suite followed
    by another edit is stale evidence."""
    ev = Evidence()
    turn = turn_messages(messages)
    pending: dict[str, tuple[str, str, int]] = {}  # tool_use_id -> (kind, command, pos)
    verify_change_passes: list[int] = []  # positions of VERIFY PASS results
    last_edit_pos = -1
    for pos, m in enumerate(turn):
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype in ("tool_use", "function_call"):
                name = str(b.get("name") or "")
                tid = str(b.get("id") or b.get("call_id") or "")
                inp = _tool_input(b)
                if name in _EDIT_TOOLS:
                    path = str(inp.get("path") or "")
                    if path and is_code_path(path):
                        if path not in ev.edited_paths:
                            ev.edited_paths.append(path)
                        last_edit_pos = pos
                elif name in _COMMAND_TOOLS:
                    cmd = str(inp.get(_COMMAND_TOOLS[name]) or "")
                    kind = classify_verify_command(cmd)
                    if kind:
                        pending[tid] = (kind, cmd, pos)
                elif name == "verify_change":
                    pending[tid] = ("verify_change", "", pos)
            elif btype in ("tool_result", "function_call_output"):
                tid = str(b.get("tool_use_id") or b.get("call_id") or "")
                entry = pending.pop(tid, None)
                if not entry:
                    continue
                kind, cmd, run_pos = entry
                text = _result_text(b)
                if kind == "verify_change":
                    if re.search(r"\bVERIFY PASS\b", text):
                        verify_change_passes.append(run_pos)
                    continue
                ev.runs.append(
                    VerifyRun(
                        kind=kind,
                        command=cmd,
                        passed=not result_failed(text),
                        fresh=False,
                        pos=run_pos,
                    )
                )
    for r in ev.runs:
        r.fresh = r.pos > last_edit_pos
    ev.verify_change_pass = any(p > last_edit_pos for p in verify_change_passes)
    return ev


def suggest_commands(workspace: str | None) -> list[str]:
    """Canonical verification commands the workspace's own files imply."""
    if not workspace or not os.path.isdir(workspace):
        return []
    out: list[str] = []

    def has(*names: str) -> bool:
        return any(os.path.exists(os.path.join(workspace, n)) for n in names)

    if has("pyproject.toml", "pytest.ini", "setup.cfg", "tests", "conftest.py"):
        out.append("python -m pytest -q")
    if has("ruff.toml", ".ruff.toml") or _pyproject_mentions(workspace, "[tool.ruff"):
        out.append("ruff check .")
    pkg = os.path.join(workspace, "package.json")
    if os.path.isfile(pkg):
        try:
            with open(pkg, encoding="utf-8") as f:
                scripts = json.load(f).get("scripts") or {}
        except (OSError, ValueError, AttributeError):
            scripts = {}
        if "test" in scripts:
            out.append("npm test")
        if "lint" in scripts:
            out.append("npm run lint")
        if has("tsconfig.json"):
            out.append("npx tsc --noEmit")
    if has("Cargo.toml"):
        out.append("cargo test")
    if has("go.mod"):
        out.append("go test ./...")
    return out[:4]


def _pyproject_mentions(workspace: str, needle: str) -> bool:
    path = os.path.join(workspace, "pyproject.toml")
    try:
        with open(path, encoding="utf-8") as f:
            return needle in f.read()
    except OSError:
        return False


def verify_nudges_used(messages: list) -> int:
    """How many verify nudges the gate already issued this turn."""
    n = 0
    for m in turn_messages(messages):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        text = content if isinstance(content, str) else ""
        if isinstance(content, list):
            text = " ".join(
                str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("text")
            )
        if text.startswith(f"[completion gate] {VERIFY_MARKER}"):
            n += 1
    return n


def verify_on_stop_feedback(
    messages: list,
    workspace: str | None,
    *,
    max_attempts: int = MAX_VERIFY_NUDGES,
) -> str | None:
    """Gate feedback when code was edited this turn without a fresh passing
    verification, or None when there is nothing to ask for."""
    ev = turn_evidence(messages)
    if not ev.edited_paths or ev.fresh_pass:
        return None
    if verify_nudges_used(messages) >= max_attempts:
        return None
    shown = ", ".join(ev.edited_paths[:8]) + (
        f" and {len(ev.edited_paths) - 8} more" if len(ev.edited_paths) > 8 else ""
    )
    if ev.fresh_failures:
        last = ev.fresh_failures[-1]
        status = f"the last verification command failed ({last.kind}: {last.command[:120]})"
    elif ev.runs:
        status = "the only verification runs happened before your latest edit"
    else:
        status = "no test, lint, typecheck or build command ran afterwards"
    suggestions = suggest_commands(workspace)
    if suggestions:
        how = (
            "Run the relevant check now (for example "
            + ", ".join(f"`{s}`" for s in suggestions)
            + ")"
        )
    else:
        how = (
            "Run the project's test, lint or build command now, or a focused temporary "
            "script that exercises the changed behaviour"
        )
    return (
        f"{VERIFY_MARKER} you edited code this turn ({shown}) but {status}. {how}, read "
        "any failure, fix the code, and report exactly what passed. If verification is "
        "not possible here, say what blocks it instead of presenting the change as verified."
    )


__all__ = [
    "MAX_VERIFY_NUDGES",
    "VERIFY_MARKER",
    "Evidence",
    "VerifyRun",
    "classify_verify_command",
    "is_code_path",
    "result_failed",
    "suggest_commands",
    "turn_evidence",
    "verify_nudges_used",
    "verify_on_stop_feedback",
]
