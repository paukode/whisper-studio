"""Agent runtime hygiene: two medium findings.

(A) The runtime tool loop must inject the internal __session_id__ into a COPY
    of the model's tool input. tu["input"] is the exact dict replayed inside the
    assistant message every turn; mutating it leaks __session_id__ into the
    transcript (where the model can imitate it), and executors that only .get()
    it never clean it up. _with_session_id() returns a copy and leaves the
    original pristine.

(B) The in-memory agent registry only ever grew: register()/update_status()
    added entries but nothing pruned them. cleanup_completed() removes completed
    agents older than its age threshold; it must drop stale ones while keeping
    recent and still-running agents.
"""

import time

from server.agents.registry import AgentRegistry
from server.agents.runtime import _with_session_id

# --- (A) input-copy leaves the model's tool_input pristine ---


def test_with_session_id_returns_a_copy_without_touching_original():
    original = {"path": "notes.txt", "mode": "read"}

    call_input = _with_session_id(original, "sess-abc")

    # The copy carries the internal id for the executor...
    assert call_input["__session_id__"] == "sess-abc"
    assert call_input["path"] == "notes.txt"
    assert call_input["mode"] == "read"
    # ...but the original (== tu["input"], replayed in the transcript) is
    # untouched: no internal key, and it is a distinct object.
    assert "__session_id__" not in original
    assert call_input is not original


def test_with_session_id_survives_executor_pop():
    # Many executors do tool_input.pop("__session_id__", ...). That must not
    # reach back into tu["input"], so simulate an executor mutating the copy.
    original = {"query": "hello"}

    call_input = _with_session_id(original, "sess-xyz")
    call_input.pop("__session_id__", None)  # executor consumes/strips it

    assert original == {"query": "hello"}
    assert "__session_id__" not in original


# --- (B) cleanup_completed prunes stale entries, keeps recent + active ---


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
