"""WS-J adversarial-review regression tests.

Pins the confirmed review fixes: string-aware JSON extraction, bare-array
findings, crash→failed lifecycle + durable task_event, the outcome `polls`
contract, the phantom-run guard, the leading-dash branch guard, the delimited
fix-agent prompt, and the bounded autofix budget.
"""

from __future__ import annotations

import pytest

from server.ci import autofix, diagnose, manager, provider, watcher


# ── diagnose: robust JSON extraction (HIGH #1, MED #3) ─────────────────────
def test_extract_json_survives_braces_inside_strings(monkeypatch):
    # An error_excerpt with a `}` used to truncate the naive brace-counter.
    reply = '{"findings":[{"check":"tsc","category":"type","summary":"bad","error_excerpt":"Type \'{ x: number }\' not assignable","suggested_fix":"widen"}]}'
    monkeypatch.setattr(diagnose, "_one_shot", lambda *a, **k: reply)
    out = diagnose.diagnose({}, "log")
    assert len(out) == 1 and out[0]["check"] == "tsc"


def test_extract_json_accepts_bare_array(monkeypatch):
    reply = '[{"check":"lint","category":"lint","summary":"s","suggested_fix":"f"}]'
    monkeypatch.setattr(diagnose, "_one_shot", lambda *a, **k: reply)
    out = diagnose.diagnose({}, "log")
    assert len(out) == 1 and out[0]["category"] == "lint"


def test_extract_json_pure_json_fast_path():
    val = diagnose._extract_json('  {"a": "}"}  ')
    assert val == {"a": "}"}


def test_extract_json_prose_wrapped_with_internal_brace():
    val = diagnose._extract_json('sure: {"error_excerpt":"missing } here"} done')
    assert val == {"error_excerpt": "missing } here"}


# ── manager: crash → failed, cancel → stopped, durable task_event (concurrency) ──
def _capture_finish(monkeypatch):
    events = []
    monkeypatch.setattr(
        manager.registry, "finish_task", lambda tid, **k: {"task_id": tid, "status": k["status"]}
    )
    monkeypatch.setattr(manager, "_emit_result", lambda *a, **k: None)
    monkeypatch.setattr(
        manager, "_emit_task_event", lambda sid, ev, task: events.append((ev, task["status"]))
    )
    return events


def test_finish_crash_is_failed(monkeypatch):
    events = _capture_finish(monkeypatch)
    manager._finish("t1", "s1", "b", {"status": "error", "error": "boom"})
    assert events == [("task_failed", "failed")]


def test_finish_empty_outcome_is_failed(monkeypatch):
    events = _capture_finish(monkeypatch)
    manager._finish("t1", "s1", "b", {})
    assert events == [("task_failed", "failed")]


def test_finish_cancelled_is_stopped(monkeypatch):
    events = _capture_finish(monkeypatch)
    manager._finish("t1", "s1", "b", {"cancelled": True, "status": "in_progress"})
    assert events == [("task_stopped", "stopped")]


def test_finish_terminal_is_completed(monkeypatch):
    events = _capture_finish(monkeypatch)
    manager._finish("t1", "s1", "b", {"status": "completed", "conclusion": "failure"})
    assert events == [("task_completed", "completed")]


# ── watcher: outcome contract + phantom-run guard (LOW #4, #6) ─────────────
def test_outcome_always_has_polls():
    assert "polls" in watcher._outcome({"run_id": 1, "status": "completed"}, polls=3)
    assert watcher._outcome(None, polls=0)["polls"] == 0


def test_status_snapshot_guards_null_run_id(monkeypatch):
    monkeypatch.setattr(provider, "gh_available", lambda: True)
    monkeypatch.setattr(provider, "latest_run", lambda *a, **k: {"run_id": None})
    snap = manager.status_snapshot("b", "/repo")
    assert snap["run"] is None


# ── provider: leading-dash branch guard (security LOW #2) ──────────────────
def test_safe_branch_rejects_leading_dash():
    assert provider._safe_branch("--web") == "HEAD"
    assert provider._safe_branch("-rf") == "HEAD"
    assert provider._safe_branch("feat/x") == "feat/x"


# ── autofix: delimited untrusted data + bounded budget (security MED #1/#3) ─
def test_fix_prompt_delimits_untrusted_diagnosis():
    src = autofix.build_autofix_script(
        "b", [{"check": "c", "category": "test", "summary": "s", "suggested_fix": "f"}]
    )
    assert "BEGIN DIAGNOSIS DATA" in src and "untrusted" in src.lower()


def test_plan_budget_is_bounded(monkeypatch):
    run = {"run_id": 1, "branch": "b", "jobs": [{"name": "J", "conclusion": "failure"}]}
    monkeypatch.setattr(autofix.provider, "failing_log", lambda *a, **k: "log")
    monkeypatch.setattr(
        autofix.diagnose,
        "diagnose",
        lambda *a, **k: [{"check": "J", "category": "test", "summary": "s", "suggested_fix": "f"}],
    )
    plan = autofix.plan_autofix(run, "/repo")
    assert plan["budget_usd"] == autofix.AUTOFIX_BUDGET_USD and plan["budget_usd"] > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
