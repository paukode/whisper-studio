"""Task-tracker single-active invariant and resume-context injection.

Covers the three fixes for the "refresh mid-plan then continue" bug:
  - Bug 3: update_task to in_progress demotes any other in_progress task.
  - Bug 2: get_session_tasks self-heals a session left with >1 in_progress
    (a zombie from an interrupted turn), keeping the most recent active task.
  - Bug 1: the system prompt injects the existing task list with IDs so a
    resumed turn continues the plan instead of recreating it.
"""

import server.tasks_tracker as tt


def _fresh(monkeypatch, tmp_path, session_id):
    monkeypatch.setattr(tt, "_DB_PATH", str(tmp_path / "tasks.db"))
    tt._task_store.pop(session_id, None)


def test_update_to_in_progress_demotes_others(monkeypatch, tmp_path):
    sid = "sess-demote"
    _fresh(monkeypatch, tmp_path, sid)
    a = tt.create_task(sid, "task A")
    b = tt.create_task(sid, "task B")

    tt.update_task(sid, a["id"], status="in_progress")
    tt.update_task(sid, b["id"], status="in_progress")

    by_id = {t["id"]: t for t in tt.get_session_tasks(sid)}
    assert by_id[b["id"]]["status"] == "in_progress"
    assert by_id[a["id"]]["status"] == "pending"
    assert sum(t["status"] == "in_progress" for t in by_id.values()) == 1


def test_get_session_tasks_self_heals_zombie(monkeypatch, tmp_path):
    sid = "sess-zombie"
    _fresh(monkeypatch, tmp_path, sid)
    a = tt.create_task(sid, "zombie A")
    b = tt.create_task(sid, "live B")

    # Simulate a pre-fix corrupted state: two in_progress written directly,
    # as if a page refresh froze A while a resumed turn started B.
    for t in tt._task_store[sid]:
        t["status"] = "in_progress"
        t["updated_at"] = 100.0 if t["id"] == a["id"] else 200.0  # B is newer

    healed = {t["id"]: t for t in tt.get_session_tasks(sid)}
    assert healed[b["id"]]["status"] == "in_progress"  # most recent stays active
    assert healed[a["id"]]["status"] == "pending"  # zombie demoted


def test_completed_tasks_are_never_demoted(monkeypatch, tmp_path):
    sid = "sess-completed"
    _fresh(monkeypatch, tmp_path, sid)
    a = tt.create_task(sid, "done A")
    b = tt.create_task(sid, "active B")
    tt.update_task(sid, a["id"], status="completed")
    tt.update_task(sid, b["id"], status="in_progress")

    by_id = {t["id"]: t for t in tt.get_session_tasks(sid)}
    assert by_id[a["id"]]["status"] == "completed"
    assert by_id[b["id"]]["status"] == "in_progress"


def test_prompt_injects_existing_tasks(monkeypatch, tmp_path):
    sid = "sess-prompt"
    _fresh(monkeypatch, tmp_path, sid)
    task = tt.create_task(sid, "Fix the thing")

    from server.prompts import build_system_prompt

    prompt = build_system_prompt(ws_path=None, session_id=sid)

    assert "EXISTING TASKS" in prompt
    assert task["id"] in prompt  # the model gets the real id to update
    assert "Fix the thing" in prompt


def test_prompt_omits_task_list_when_none(monkeypatch, tmp_path):
    sid = "sess-empty"
    _fresh(monkeypatch, tmp_path, sid)

    from server.prompts import build_system_prompt

    prompt = build_system_prompt(ws_path=None, session_id=sid)

    assert "TASK TRACKING" in prompt  # base guidance always present
    assert "EXISTING TASKS" not in prompt  # nothing injected for an empty plan
