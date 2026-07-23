"""User-editable global prompt rules (root ``PROMPT_RULES.md``).

The file is injected into the assistant's prompts app-wide, so the user can shape
how it writes (tone, formatting, what to avoid) or empty the file to lift every
restriction. Lines starting with ``#`` are treated as notes and dropped. Cached
with an mtime check so edits apply on the next request without a restart.
"""

import logging
import os
import threading

log = logging.getLogger("whisper-studio")

# repo root = .../server/prompts/rules.py -> up three
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
RULES_PATH = os.path.join(_ROOT, "PROMPT_RULES.md")

_lock = threading.Lock()
_cache: dict = {"text": "", "mtime": -1.0}


def load_prompt_rules() -> str:
    """The user's rules with comment/blank lines stripped, or '' if none.

    Missing or empty file → '' (no rules, no restrictions).
    """
    try:
        mtime = os.path.getmtime(RULES_PATH)
    except OSError:
        with _lock:
            _cache["text"] = ""
            _cache["mtime"] = -1.0
        return ""

    with _lock:
        if mtime == _cache["mtime"]:
            return _cache["text"]

    try:
        with open(RULES_PATH, encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        log.warning("prompt rules: could not read %s: %s", RULES_PATH, e)
        return ""

    lines = [
        s
        for line in raw.splitlines()
        if (s := line.strip()) and not s.startswith("#") and not s.startswith("<!--")
    ]
    text = "\n".join(lines).strip()

    with _lock:
        _cache["text"] = text
        _cache["mtime"] = mtime
    return text


def rules_block() -> str:
    """The rules formatted as a prompt section, or '' if no rules are set."""
    rules = load_prompt_rules()
    if not rules:
        return ""
    return (
        "## Output rules\n"
        "The user configured these global rules. Follow them unless the user "
        "explicitly asks otherwise in their message:\n" + rules
    )


def append_rules(prompt: str) -> str:
    """Append the user's rules block to a system prompt (no-op if none set)."""
    block = rules_block()
    return f"{prompt}\n\n{block}" if block else prompt
