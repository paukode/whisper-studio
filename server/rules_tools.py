"""``rules_write``: the assistant records the user's rules and instructions.

The files live under ``~/.whisper`` (see server/prompts/rules.py for the
layout), so a write here needs no approval in any permission mode but plan:
this is the app's own folder, the place the user asked these to go. In a
plan-mode turn the tool raises an approval card instead (the user's rule:
memory may still update in plan mode, a rule needs their yes), showing the
file as it would be after the write; the card's Yes runs ``_do_rules_write``
with exactly that content. The tool is in the catalog on every turn and core
in chat, because "remember this" is said in chat and a deferred tool could
miss it.

Routing the model is told to follow: a behaviour the user wants kept
("remember this", "my rule is", "from now on") is a rule, one imperative line.
It goes to the project's ``rules.md`` while a workspace is connected, and to
the global ``rules.md`` when the user says always, everywhere, all the time or
there is no workspace. A fact about the user or the project that they ask to
have noted goes to the matching ``WHISPER.md``. What the assistant notices on
its own belongs to memory, never here.
"""

from __future__ import annotations

import json
import logging
import os

from server.executors import register_executor
from server.prompts.rules import (
    RULES_FILENAME,
    global_profile_path,
    global_rules_dir,
    project_profile_path,
    project_rules_dir,
)

log = logging.getLogger("whisper-studio")

TARGETS = ("project_rules", "global_rules", "project_whisper_md", "global_whisper_md")
MODES = ("append", "replace")

RULES_TOOLS: list[dict] = [
    {
        "name": "rules_write",
        "description": (
            "Record a rule or instruction the user gave you, in their own files under "
            "~/.whisper (asks for approval in plan mode only). Use it when the user says 'remember this', "
            "'my rule is', 'from now on', 'do not do that again' or asks you to update "
            "their rules or instructions. Targets: project_rules (rules for the connected "
            "workspace only, the default), global_rules (when the user says always, "
            "everywhere, all the time, in every project, or no workspace is connected), "
            "project_whisper_md (facts about this project: what it is, layout, how to "
            "build and test) and global_whisper_md (facts about the user: role, stack, "
            "preferences). Rules are one imperative line each, condensed from the user's "
            "words; append adds lines and skips ones already present, replace rewrites "
            "the whole file (use it to remove or reword a rule, keeping the rest). What "
            "you notice on your own goes to memory_write, not here."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "enum": list(TARGETS),
                    "description": "Which file to write.",
                },
                "content": {
                    "type": "string",
                    "description": (
                        "The rule lines (one per line) or the instructions text to add, or the "
                        "complete new file content with mode 'replace'."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": list(MODES),
                    "description": "append (default) or replace.",
                },
            },
            "required": ["target", "content"],
        },
    }
]

RULES_TOOL_NAMES = frozenset(t["name"] for t in RULES_TOOLS)


def _target_path(target: str) -> tuple[str, str]:
    """(absolute path, error). Project targets need a connected workspace."""
    if target in ("global_rules", "global_whisper_md"):
        path = (
            os.path.join(global_rules_dir(), RULES_FILENAME)
            if target == "global_rules"
            else global_profile_path()
        )
        return path, ""
    from server.workspace import get_workspace_path

    ws = get_workspace_path()
    if not ws:
        return "", (
            "no workspace is connected, so there is no project to record this for. "
            "Use a global target, or ask the user to connect the workspace."
        )
    path = (
        os.path.join(project_rules_dir(ws), RULES_FILENAME)
        if target == "project_rules"
        else project_profile_path(ws)
    )
    return path, ""


def _display(path: str) -> str:
    from server.infrastructure.paths import user_root

    root = user_root()
    if path.startswith(root + os.sep):
        return "~/.whisper/" + os.path.relpath(path, root)
    return path


def _as_rule_lines(content: str) -> list[str]:
    """One bullet per non-empty line; headings and existing bullets pass through."""
    out = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("-", "*", "#")):
            out.append(line)
        else:
            out.append(f"- {line}")
    return out


def _prepare(tool_input: dict) -> tuple[dict | None, str]:
    """Work out the write: ``({path, target, mode, original, content, added},
    "")`` or ``(None, message)`` when there is nothing to write (an input
    error, a secret, a line already present)."""
    target = str(tool_input.get("target", "")).strip()
    content = tool_input.get("content", "")
    mode = str(tool_input.get("mode", "") or "append").strip()

    if target not in TARGETS:
        return None, f"Error: target must be one of {', '.join(TARGETS)}."
    if mode not in MODES:
        return None, f"Error: mode must be one of {', '.join(MODES)}."
    if not isinstance(content, str) or not content.strip():
        return None, "Error: content is required."

    from server.memory.secret_scanner import check_and_block

    clean, _redacted, findings = check_and_block(content)
    if not clean:
        labels = ", ".join(f["label"] for f in findings)
        return (
            None,
            f"BLOCKED: the content contains potential secrets ({labels}). Remove them first.",
        )

    path, err = _target_path(target)
    if err:
        return None, f"Error: {err}"

    existing = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            existing = f.read()
    except FileNotFoundError:
        pass
    except OSError as e:
        return None, f"Error reading {_display(path)}: {e}"

    is_rules = target.endswith("_rules")
    added = None
    if mode == "replace":
        new_text = content.strip() + "\n"
    elif is_rules:
        present = {line.strip() for line in existing.splitlines()}
        lines = [line for line in _as_rule_lines(content) if line not in present]
        if not lines:
            return None, f"Nothing to add: every line is already in {_display(path)}."
        body = existing.rstrip("\n")
        new_text = (body + "\n" if body else "") + "\n".join(lines) + "\n"
        added = len(lines)
    else:
        block = content.strip()
        if block in existing:
            return None, f"Nothing to add: that text is already in {_display(path)}."
        body = existing.rstrip("\n")
        new_text = (body + "\n\n" if body else "") + block + "\n"
    return {
        "path": path,
        "target": target,
        "mode": mode,
        "original": existing,
        "content": new_text,
        "added": added,
    }, ""


def _write(path: str, new_text: str, target: str, mode: str, added: int | None) -> str:
    from server.workspace.paths import _atomic_write_text

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _atomic_write_text(path, new_text)
    except OSError as e:
        return f"Error writing {_display(path)}: {e}"

    log.info("Rules written: target=%s mode=%s path=%s", target, mode, path)
    if mode == "replace":
        return f"Rewrote {_display(path)}. It applies from the next turn."
    if added is not None:
        noun = "rule" if added == 1 else "rules"
        return f"Added {added} {noun} to {_display(path)}. They apply from the next turn."
    return f"Added to {_display(path)}. It applies from the next turn."


@register_executor("rules_write", read_only=False, concurrent_safe=False)
def execute_rules_write(tool_input: dict, transcript: str = "", attachments=None) -> str:
    plan, message = _prepare(tool_input)
    if plan is None:
        return message

    from server.agents.tool_access import started_read_only

    if started_read_only():
        # A plan-mode turn: the user sees the file as it would be and decides.
        payload = {
            "action": "rules_write",
            "target": plan["target"],
            "mode": plan["mode"],
            "original": plan["original"],
            "content": plan["content"],
            "added": plan["added"],
        }
        return f"[WS_APPROVAL]{json.dumps(payload)}"
    return _write(plan["path"], plan["content"], plan["target"], plan["mode"], plan["added"])


def _summary(payload: dict) -> str:
    target = str(payload.get("target", ""))
    path, err = _target_path(target) if target in TARGETS else ("", "unknown target")
    name = _display(path) if not err else target or "?"
    if payload.get("mode") == "replace":
        return f"Rewrite {name}"
    added = payload.get("added")
    if isinstance(added, int):
        return f"Add {added} {'rule' if added == 1 else 'rules'} to {name}"
    return f"Add to {name}"


async def _do_rules_write(payload: dict):
    """The card's Yes: write the content the card showed. The path comes from
    the target, never from the payload, so a forged payload cannot aim this
    at another file."""
    from server.approval.spec import ApprovalOutcome

    target = str(payload.get("target", ""))
    mode = str(payload.get("mode", "") or "append")
    content = payload.get("content")
    if target not in TARGETS or mode not in MODES or not isinstance(content, str):
        return ApprovalOutcome(ok=False, error="malformed rules_write approval")
    path, err = _target_path(target)
    if err:
        return ApprovalOutcome(ok=False, error=err)
    added = payload.get("added")
    output = _write(path, content, target, mode, added if isinstance(added, int) else None)
    if output.startswith("Error"):
        return ApprovalOutcome(ok=False, error=output)
    return ApprovalOutcome(ok=True, output=output)


def register_rules_approval() -> None:
    """The ApprovalSpec behind a plan-mode rules_write card (called from
    server.approval.bootstrap.register_defaults). Category ``write``: the
    card shows the rules file as a diff, and the resolver asks in plan mode
    because the payload carries no path for the app-storage exemption."""
    from server.approval import register
    from server.approval.spec import ApprovalSpec

    register(
        "rules_write",
        ApprovalSpec(
            category="write",
            preview="diff",
            summary=_summary,
            executor=_do_rules_write,
            risk_hint="low",
            payload_fields=["target", "mode", "original", "content", "added"],
        ),
    )
