"""rules_write: the assistant records the user's rules under ~/.whisper.

"Remember this" appends one imperative line to the project's rules.md (global
rules.md when the user says always or no workspace is connected); a fact the
user asks to have noted goes to the matching WHISPER.md. Writes land in the
user's own folder, so they go through no approval; the tool is always in the
catalog and core in chat so the request is never deferred behind tool_search.
"""

import os

from server.agents.config import WRITE_TOOLS
from server.chat.tool_partition import CORE_TOOLS
from server.prompts.rules import global_profile_path, global_rules_dir, project_rules_dir
from server.rules_tools import RULES_TOOLS, execute_rules_write


def _connect(monkeypatch, ws):
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: ws)


def _read(path):
    with open(path) as f:
        return f.read()


def test_tool_is_always_core_and_counted_as_a_write():
    assert "rules_write" in CORE_TOOLS
    assert "rules_write" in WRITE_TOOLS
    assert RULES_TOOLS[0]["input_schema"]["required"] == ["target", "content"]


def test_project_rule_lands_in_the_workspace_folder(tmp_path, monkeypatch):
    ws = str(tmp_path / "proj")
    os.makedirs(ws)
    _connect(monkeypatch, ws)
    out = execute_rules_write(
        {"target": "project_rules", "content": "Run the suite before merging"}
    )
    path = os.path.join(project_rules_dir(ws), "rules.md")
    assert "Added 1 rule" in out and "~/.whisper/rules/" in out
    assert _read(path) == "- Run the suite before merging\n"


def test_append_skips_lines_already_present(tmp_path, monkeypatch):
    ws = str(tmp_path / "proj")
    os.makedirs(ws)
    _connect(monkeypatch, ws)
    execute_rules_write({"target": "project_rules", "content": "- No emoji"})
    out = execute_rules_write({"target": "project_rules", "content": "No emoji\nUse uv"})
    assert "Added 1 rule" in out
    assert _read(os.path.join(project_rules_dir(ws), "rules.md")) == "- No emoji\n- Use uv\n"
    assert execute_rules_write({"target": "project_rules", "content": "Use uv"}).startswith(
        "Nothing to add"
    )


def test_global_rule_needs_no_workspace(monkeypatch):
    _connect(monkeypatch, None)
    out = execute_rules_write({"target": "global_rules", "content": "Always answer in English"})
    assert "Added 1 rule" in out
    assert _read(os.path.join(global_rules_dir(), "rules.md")) == "- Always answer in English\n"


def test_project_targets_refuse_without_a_workspace(monkeypatch):
    _connect(monkeypatch, None)
    out = execute_rules_write({"target": "project_rules", "content": "x"})
    assert out.startswith("Error:") and "no workspace" in out


def test_profile_text_appends_as_a_paragraph(monkeypatch):
    _connect(monkeypatch, None)
    execute_rules_write({"target": "global_whisper_md", "content": "# Me\n\nData engineer."})
    out = execute_rules_write({"target": "global_whisper_md", "content": "Works on AWS."})
    assert out.startswith("Added to")
    assert _read(global_profile_path()) == "# Me\n\nData engineer.\n\nWorks on AWS.\n"


def test_replace_rewrites_the_file(monkeypatch):
    _connect(monkeypatch, None)
    execute_rules_write({"target": "global_rules", "content": "Old rule"})
    out = execute_rules_write(
        {"target": "global_rules", "content": "- New rule", "mode": "replace"}
    )
    assert out.startswith("Rewrote")
    assert _read(os.path.join(global_rules_dir(), "rules.md")) == "- New rule\n"


def test_secrets_are_blocked(monkeypatch):
    _connect(monkeypatch, None)
    out = execute_rules_write(
        {"target": "global_rules", "content": "Use AKIAIOSFODNN7EXAMPLE as the key"}
    )
    assert out.startswith("BLOCKED")
    assert not os.path.exists(os.path.join(global_rules_dir(), "rules.md"))


def test_bad_inputs_are_errors(monkeypatch):
    _connect(monkeypatch, None)
    assert execute_rules_write({"target": "nowhere", "content": "x"}).startswith("Error:")
    assert execute_rules_write({"target": "global_rules", "content": "  "}).startswith("Error:")
    assert execute_rules_write(
        {"target": "global_rules", "content": "x", "mode": "zap"}
    ).startswith("Error:")


# ── plan mode: the write waits on a card ────────────────────────────────────
# The user's rule: memory may update in plan mode with no card, a rule needs
# their yes. The tool raises the card itself, showing the file as it would be;
# the card's Yes writes exactly that, and the resolver asks in plan mode.


def _in_plan_mode(fn, *args):
    from server.agents.tool_access import read_only_scope

    token = read_only_scope.set(True)
    try:
        return fn(*args)
    finally:
        read_only_scope.reset(token)


def _approval(out):
    import json

    assert out.startswith("[WS_APPROVAL]"), out
    return json.loads(out[len("[WS_APPROVAL]") :])


def test_plan_mode_raises_a_card_showing_the_file_as_it_would_be(monkeypatch):
    path = os.path.join(global_rules_dir(), "rules.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("- Keep it short\n")
    payload = _approval(
        _in_plan_mode(execute_rules_write, {"target": "global_rules", "content": "Never guess"})
    )
    assert payload["action"] == "rules_write"
    assert payload["original"] == "- Keep it short\n"
    assert payload["content"] == "- Keep it short\n- Never guess\n"
    assert payload["added"] == 1 and payload["mode"] == "append"
    assert _read(path) == "- Keep it short\n"  # nothing written yet


def test_plan_mode_card_asks_and_other_modes_write_directly(monkeypatch):
    from server.security.permissions import MODE_DEFAULT, MODE_PLAN, resolve_static_decision

    monkeypatch.setattr(
        "server.security.permissions.load_permissions",
        lambda: {"mode": MODE_PLAN, "rules": [], "category_modes": {}},
    )
    payload = _approval(
        _in_plan_mode(execute_rules_write, {"target": "global_rules", "content": "Never guess"})
    )
    assert resolve_static_decision("rules_write", payload, "write", {}, MODE_PLAN) == "ask"
    out = execute_rules_write({"target": "global_rules", "content": "Never guess"})
    assert out.startswith("Added 1 rule"), out
    assert (
        resolve_static_decision("ws_write_file", {"path": "a"}, "write", {}, MODE_DEFAULT) == "ask"
    )


def test_the_cards_yes_writes_what_the_card_showed(monkeypatch):
    import asyncio

    from server.approval import registry
    from server.approval.bootstrap import register_defaults
    from server.rules_tools import _do_rules_write

    register_defaults()
    spec = registry.get("rules_write")
    assert spec is not None and spec.category == "write" and spec.preview == "diff"
    payload = _approval(
        _in_plan_mode(execute_rules_write, {"target": "global_rules", "content": "Never guess"})
    )
    payload = spec.build_payload(payload)
    assert "path" not in payload
    # A forged path is ignored: the file comes from the target.
    outcome = asyncio.run(_do_rules_write({**payload, "path": "/tmp/elsewhere.md"}))
    assert outcome.ok, outcome.error
    assert "Added 1 rule" in outcome.output
    assert _read(os.path.join(global_rules_dir(), "rules.md")).endswith("- Never guess\n")
    assert not os.path.exists("/tmp/elsewhere.md")


def test_an_error_needs_no_card_even_in_plan_mode(monkeypatch):
    out = _in_plan_mode(execute_rules_write, {"target": "nope", "content": "x"})
    assert out.startswith("Error:")
    _connect(monkeypatch, None)
    out = _in_plan_mode(execute_rules_write, {"target": "project_rules", "content": "x"})
    assert out.startswith("Error:")
