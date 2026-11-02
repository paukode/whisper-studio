"""The in-memory agent registry prunes finished agents: cleanup_completed()
removes completed agents older than its age threshold and keeps recent and
still-running ones."""

import time

from server.agents.registry import AgentRegistry


def test_cleanup_completed_prunes_old_keeps_recent_and_active():
    reg = AgentRegistry()

    # Stale completed agent (finished well beyond the default 1h threshold).
    reg.register("old", "general", "old task", session_id="s1")
    reg.update_status("old", "completed", "done")
    reg.get("old").completed_at = time.time() - 7200  # 2 hours ago

    # Still-running agent — never pruned regardless of age.
    reg.register("active", "general", "running task", session_id="s1")

    # Recently completed agent — kept until it ages past the threshold.
    reg.register("recent", "general", "recent task", session_id="s1")
    reg.update_status("recent", "completed", "just finished")

    removed = reg.cleanup_completed()  # default max_age_seconds=3600

    assert removed == 1
    assert reg.get("old") is None
    assert reg.get("active") is not None
    assert reg.get("recent") is not None


def test_cleanup_completed_respects_custom_threshold():
    reg = AgentRegistry()

    reg.register("done", "general", "task", session_id="s1")
    reg.update_status("done", "completed", "result")
    reg.get("done").completed_at = time.time() - 120  # 2 minutes ago

    # 5-minute threshold: nothing pruned yet.
    assert reg.cleanup_completed(max_age_seconds=300) == 0
    assert reg.get("done") is not None

    # 1-minute threshold: now it is stale.
    assert reg.cleanup_completed(max_age_seconds=60) == 1
    assert reg.get("done") is None


def test_cleanup_completed_never_prunes_running_agents():
    reg = AgentRegistry()

    reg.register("runner", "general", "long task", session_id="s1")
    # Backdate creation far into the past; running agents have no completed_at
    # and must survive any sweep.
    reg.get("runner").created_at = time.time() - 100000

    assert reg.cleanup_completed(max_age_seconds=1) == 0
    assert reg.get("runner") is not None
