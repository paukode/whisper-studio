"""The workspace command approval executor (_do_command) sandboxes but must
also validate — so an approved command can't bypass the dangerous-pattern /
sensitive-path checks (e.g. reading ~/.ssh)."""

import asyncio

from server.approval.bootstrap import _do_command


def test_do_command_rejects_dangerous(monkeypatch):
    # Pretend a workspace is connected so validation (not the no-workspace
    # guard) is what rejects the command.
    from server import workspace

    monkeypatch.setattr(workspace, "get_workspace_path", lambda: "/tmp")
    outcome = asyncio.run(_do_command({"command": "cat ~/.ssh/id_rsa"}))
    assert outcome.ok is False
    assert outcome.error
