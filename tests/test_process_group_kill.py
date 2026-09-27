"""kill_process_group must reach the whole group even after its leader exited.

A shell that backgrounds work (``npm run dev &``, ``(...) &``) and returns
leaves its children running in the group it led. The kill used to look the
group up from the leader (``getpgid``), which fails once the leader is gone,
so those children survived every timeout and Stop, untracked.
"""

import asyncio
import os
import signal
import subprocess
import time

from server.process_utils import (
    kill_process_group,
    kill_process_group_async,
    new_process_group,
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A reparented zombie still answers kill(0); ps knows better.
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(out.stdout.strip()) and not out.stdout.strip().startswith("Z")


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _cleanup(*pids: int) -> None:
    """Never leave a test sleeper behind, whatever the assertion said."""
    for pid in pids:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def _spawn_orphaning_leader(tmp_path, body: str) -> tuple[subprocess.Popen, int]:
    """A group leader that backgrounds ``body`` and exits at once."""
    pid_file = tmp_path / "child.pid"
    proc = subprocess.Popen(
        ["/bin/sh", "-c", f"({body}) & echo $! > {pid_file}"],
        preexec_fn=new_process_group,
    )
    proc.wait(timeout=5)
    child = int(pid_file.read_text().strip())
    assert _alive(child), "the backgrounded child must outlive its leader for this test"
    return proc, child


def test_kill_reaches_children_after_the_leader_exited(tmp_path):
    proc, child = _spawn_orphaning_leader(tmp_path, "exec sleep 30")
    try:
        kill_process_group(proc, timeout=2)
        assert _wait_dead(child), "a backgrounded child of an exited leader survived the kill"
    finally:
        _cleanup(child)


def test_kill_escalates_when_a_leaderless_group_ignores_sigterm(tmp_path):
    """The grace wait watches the GROUP: a dead leader must not end it early
    while a member that ignores SIGTERM is still running."""
    proc, child = _spawn_orphaning_leader(tmp_path, "trap '' TERM; exec sleep 30")
    try:
        started = time.monotonic()
        kill_process_group(proc, timeout=0.5)
        assert _wait_dead(child, timeout=3), "SIGKILL escalation never reached the member"
        assert time.monotonic() - started >= 0.4, "escalated before the grace period"
    finally:
        _cleanup(child)


def test_sandboxed_timeout_kills_backgrounded_children(tmp_path):
    """run_sandboxed callers (the workspace shell endpoint, run_python,
    aws_cli, hooks): a timed-out command whose shell already exited must not
    leave its backgrounded work running. The ws_run_command paths go through
    the handoff instead (tests/test_left_running_group.py)."""
    from server.sandbox import run_sandboxed

    pid_file = tmp_path / "bgpid"
    try:
        run_sandboxed(
            f"(exec sleep 30) & echo $! > {pid_file}; echo started",
            cwd=str(tmp_path),
            timeout=1,
        )
    except subprocess.TimeoutExpired:
        pass
    else:  # pragma: no cover - the child holds stdout, so this must time out
        raise AssertionError("expected the command to time out")
    child = int(pid_file.read_text().strip())
    try:
        assert _wait_dead(child), "the timed-out command's background child survived"
    finally:
        _cleanup(child)


def test_async_kill_reaches_children_after_the_leader_exited(tmp_path):
    pid_file = tmp_path / "child.pid"

    async def scenario() -> tuple[int, int]:
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            f"(exec sleep 30) & echo $! > {pid_file}",
            start_new_session=True,
        )
        await proc.wait()
        child = int(pid_file.read_text().strip())
        assert _alive(child)
        await kill_process_group_async(proc, timeout=2)
        return proc.pid, child

    _leader, child = asyncio.run(scenario())
    try:
        assert _wait_dead(child), "the async kill left the leaderless group running"
    finally:
        _cleanup(child)


def test_kill_of_an_already_empty_group_is_a_quiet_no_op(tmp_path):
    proc = subprocess.Popen(["/bin/sh", "-c", "exit 0"], preexec_fn=new_process_group)
    proc.wait(timeout=5)
    started = time.monotonic()
    kill_process_group(proc, timeout=5)
    assert time.monotonic() - started < 1, "an empty group must not wait out the grace period"
