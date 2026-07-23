"""The per-run SSE stream must end only on the RUN-level completion.

Regression: the loop broke on any ``phase=="completed"`` event, but every
sub-agent also emits ``phase:"completed"`` on the shared ``workflow:{run_id}``
channel, so the first agent to finish cut off the whole run's live events.
"""

from server.workflows.routes import _is_run_terminal


def test_run_level_completion_is_terminal():
    # What manager._finalize publishes (wrapped as type="workflow_event").
    ev = {"type": "workflow_event", "phase": "completed", "status": "done"}
    assert _is_run_terminal(ev) is True


def test_per_agent_completion_is_not_terminal():
    # A single sub-agent finishing: raw event on the same channel, no type.
    ev = {"phase": "completed", "agent_id": "abc123", "status": "completed"}
    assert _is_run_terminal(ev) is False


def test_agent_lifecycle_events_are_not_terminal():
    assert _is_run_terminal({"type": "agent", "status": "completed"}) is False
    assert _is_run_terminal({"type": "phase", "name": "Verify"}) is False
    assert _is_run_terminal({"type": "snapshot", "run": {}}) is False
