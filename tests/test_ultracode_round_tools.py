"""The workflow tools must reach the WIRE on an ultracode turn.

``assemble_full_catalog(ultracode=True)`` was already covered directly (see
test_workflow_tools_routes.py) and passed the whole time — but nothing tested
the catalog the ENGINE builds per round, and that one dropped the flag. The
result: an ultracode turn got a system prompt ordering it to "write a workflow
script and run it with `workflow_run`" while `workflow_run` was not in its tools
array at all, so the model fell back to spawn_agent and no workflow was ever
launched.

These tests sit at that altitude: ``_assemble_round_tools`` is the function
whose return value becomes the provider request's tool list
(runner.py — ``ctx.adapter.stream_round(messages, tools, ...)``).
"""

from server.chat.engine.policy import CHAT_POLICY
from server.chat.engine.runner import TurnContext, _assemble_round_tools

WORKFLOW_TOOLS = {"workflow_run", "workflow_status", "workflow_save", "workflow_list"}


def _ctx(effort_label: str | None) -> TurnContext:
    """A chat TurnContext exactly as server/chat/routes.py builds one: no
    ``tool_catalog`` override, so the engine assembles the round's tools."""
    return TurnContext(
        session_id="sess-ultracode",
        model_key="opus5.0",
        model_id="test-model",
        messages=[],
        adapter=object(),
        policy=CHAT_POLICY,
        loop=None,
        executor=None,
        effort_label=effort_label,
    )


def _names(effort_label: str | None) -> set[str]:
    tools, _core = _assemble_round_tools(_ctx(effort_label))
    return {t["name"] for t in tools}


def test_ultracode_turn_advertises_the_workflow_tools():
    names = _names("ultracode")
    missing = WORKFLOW_TOOLS - names
    assert not missing, f"ultracode round is missing {sorted(missing)}"


def test_ultracode_turn_advertises_the_ci_tools():
    # Same gate in tool_pool: CI autofix hands its fix to the workflow runtime.
    assert {"ci_watch", "ci_status", "ci_autofix"} <= _names("ultracode")


def test_non_ultracode_turn_does_not_advertise_them():
    names = _names("high")
    assert not (WORKFLOW_TOOLS & names)
    assert "ci_autofix" not in names


def test_effortless_turn_does_not_advertise_them():
    # Haiku and the local path pass no effort label at all.
    assert not (WORKFLOW_TOOLS & _names(None))


def test_ultracode_keeps_the_ordinary_tools_too():
    # The flag ADDS to the catalog; it must not displace the normal pool.
    names = _names("ultracode")
    assert "spawn_agent" in names
    assert "task_status" in names


def test_workflow_tools_are_advertised_not_deferred():
    """They are core (tool_partition), so progressive disclosure must never
    hide them behind a tool_search round — an ultracode turn has to be able to
    call workflow_run immediately."""
    tools, core_count = _assemble_round_tools(_ctx("ultracode"))
    if core_count is None:  # progressive_tools off — everything is advertised
        return
    core = {t["name"] for t in tools[:core_count]}
    assert WORKFLOW_TOOLS <= core
