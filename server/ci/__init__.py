"""WS-J: CI watch + PR autofix.

A branch's GitHub Actions run is watched to a terminal conclusion as a
background task (rides the WS-A registry, kind='ci'); on failure the failing
logs are diagnosed and an autofix is proposed — approval-gated before it ever
writes or pushes, mirroring the WS-D workflow preview and WS-I hook trust.

Layers:
- provider.py  thin read-only ``gh`` CLI wrapper (runs / jobs / failing logs / PR)
- watcher.py   poll a run to terminal, record the task, emit ci_progress events
- diagnose.py  failing-log -> structured diagnosis (one agent per failed job)
- autofix.py   diagnose -> propose patch -> verify -> apply loop, attempt-capped
"""
