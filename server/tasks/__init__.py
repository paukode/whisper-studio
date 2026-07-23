"""Unified background-task framework.

Every long-running unit of work the server owns (background shell commands,
detached agent runs, workflow runs) is a row in the ``agent_tasks`` registry
(:mod:`server.tasks.registry`). Completion is announced into the owning chat
session through :mod:`server.tasks.events`, which generalizes the proven
cron-event persist+publish path.

Distinct from :mod:`server.tasks_tracker`, the in-conversation todo-list
feature; the two share nothing but an unlucky name.
"""
