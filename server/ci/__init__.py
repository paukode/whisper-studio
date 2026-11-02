"""CI watch and autofix for GitHub Actions.

A branch's latest Actions run is watched to a terminal conclusion as a
background task (task registry kind 'ci'). On failure, the failing logs are
diagnosed and a fix is proposed as a workflow that runs only after the user
approves it.

Modules:
- provider.py  read-only ``gh`` CLI wrapper (runs, jobs, failing logs); a gh
               failure raises GhError with gh's message
- watcher.py   poll a run to terminal and emit ci_progress events
- manager.py   detached watch lifecycle, the CI target (repository and
               branch), and the one-shot status snapshot
- diagnose.py  one model call reads the failing logs of all failed jobs and
               returns structured findings
- autofix.py   builds the approvable fix-then-verify workflow script
- tools.py     the ci_watch, ci_status and ci_autofix model tools
- routes.py    the /api/ci HTTP routes
"""
