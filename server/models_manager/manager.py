"""Download job lifecycle: start, cancel, reap, delete.

Downloads run in a SUBPROCESS (``<venv python> -m server.models_manager.worker
<key>``), not a thread: the ensure_* functions block inside huggingface_hub
with no cancellation hook, and a thread can't be killed — terminating the
worker process is a real, honest cancel. The worker calls the exact same
ensure function the app itself uses (including GLiNER's backbone-tokenizer
pre-cache), so a managed download is byte-for-byte the lazy first-use one.

State is poll-driven: there is no watcher thread. Every API call runs
``reap()`` first, which folds finished workers into terminal states (success
clears the job; failure records the log tail as the error). A cancelled
download leaves hf's ``.incomplete`` files in place so the next attempt
resumes instead of restarting.

Requests beyond the concurrency cap are QUEUED (FIFO), not refused: the key
goes into ``_queue`` and the next free slot starts it. The queue is pumped by
``reap()`` — which every endpoint (including the 1s status poll) calls first —
and directly by ``cancel()``, so a freed slot is refilled on the very next
poll at the latest and queued items can never stall.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time

from server.infrastructure.paths import data_root, repo_root
from server.models_manager import sizes
from server.models_manager.catalog import (
    GROUP_LOCAL_CHAT,
    ModelEntry,
    bytes_on_disk,
    entries,
    get_entry,
    is_installed,
    model_dir,
)

log = logging.getLogger("whisper-studio")

# At most this many downloads at once — kind to disk and network; the rest of
# the app (transcription, chat) keeps its share of bandwidth and IOPS.
MAX_CONCURRENT = 2

# A downloading job whose worker has produced no new bytes for this long is
# declared failed rather than left sitting at a stuck percent forever. A
# download that is still pulling bytes resets this clock on every reap, so a
# slow-but-progressing transfer is never flagged. Kept as a module global so a
# smaller machine can tune it and tests can shrink it.
STALL_TIMEOUT_SEC = 120.0


class Conflict(Exception):
    """A request that is valid but impossible right now (HTTP 409)."""


class _Job:
    __slots__ = (
        "key",
        "proc",
        "pid",
        "log_path",
        "started_at",
        "cancelled",
        "last_bytes",
        "last_progress_at",
    )

    def __init__(self, key: str, proc: subprocess.Popen, log_path: str) -> None:
        self.key = key
        self.proc = proc
        # The worker PID, kept explicitly so liveness can be probed directly
        # (os.kill(pid, 0)) even in the case where the Popen handle never
        # observes the exit — a dead worker must never read as "downloading".
        self.pid = getattr(proc, "pid", None)
        self.log_path = log_path
        self.started_at = time.time()
        self.cancelled = False
        # Stall accounting: the most bytes seen on disk for this run and when
        # that high-water last advanced. Starting at -1 makes the first reap
        # record a baseline (and arm the stall clock) even at zero bytes.
        self.last_bytes = -1
        self.last_progress_at = self.started_at


_jobs: dict[str, _Job] = {}
# Keys waiting for a download slot, in request (FIFO) order. A queued model
# has no worker yet: cancelling it just removes the key (partials untouched).
_queue: list[str] = []
_errors: dict[str, str] = {}  # key -> last failure, cleared on retry/delete
# key -> monotonic high-water progress fraction for the current download run.
# The raw bytes/estimate ratio can walk BACKWARDS mid-run (the real HF size
# lands and replaces a smaller fallback estimate); this mark keeps the number
# shown to the user from regressing. Cleared at every run boundary.
_progress_max: dict[str, float] = {}
_lock = threading.Lock()


def _log_path(key: str) -> str:
    d = os.path.join(data_root(), "model-downloads")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{key}.log")


def _spawn_worker(key: str, log_path: str) -> subprocess.Popen:
    """Start the download worker. Module-level so tests can stub it out."""
    cmd = [sys.executable, "-m", "server.models_manager.worker", key]
    env = {**os.environ, "HF_HUB_DISABLE_PROGRESS_BARS": "1", "PYTHONUNBUFFERED": "1"}
    with open(log_path, "w") as log_file:
        # start_new_session: the worker must survive nothing — but it must not
        # share our process group, so a Ctrl-C to the server doesn't half-kill
        # a download we then mis-report as failed.
        return subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=repo_root(),
            env=env,
            start_new_session=True,
        )


def _failure_reason(log_path: str) -> str:
    """The worker's short, actionable failure reason, or '' when there is none.

    The worker writes ``DOWNLOAD FAILED: <reason>`` as its final line before a
    non-zero exit (see worker.py), so a handled failure becomes a sentence the
    user can act on. Only that marker line is trusted: a worker that was killed
    (cancel, SIGKILL, OOM) never writes one, and the tail of its log is just
    library noise — for those the generic exit-code message is more honest.
    """
    try:
        with open(log_path, errors="replace") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except OSError:
        return ""
    marker = "DOWNLOAD FAILED:"
    for ln in reversed(lines):
        if ln.startswith(marker):
            return ln[len(marker) :].strip()
    return ""


def _failure_message(log_path: str, rc: int | None) -> str:
    """A UI-facing error: the worker's reason plus how it ended (exit code, or
    "unexpectedly" when the process vanished without an observed exit)."""
    reason = _failure_reason(log_path)
    how = "unexpectedly" if rc is None else f"with exit code {rc}"
    if reason:
        return f"Download failed ({how}): {reason}"
    return f"Download worker exited {how}. See Settings > Models for the log."


def _pid_alive(pid: int | None) -> bool:
    """Whether the worker process still exists. ``os.kill(pid, 0)`` probes the
    PID without sending a real signal — the liveness backstop for the case where
    ``Popen.poll()`` cannot see the exit. An unknown PID is assumed alive (a
    missing PID alone never condemns a job). Module-level so tests can stub it."""
    if pid is None:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, just not ours to signal
        return True
    except OSError:  # ambiguous errno — be conservative, don't declare it dead
        return True
    return True


def _note_byte_progress(job: _Job, entry: ModelEntry | None, now: float) -> bool:
    """Fold the current on-disk size into the job's stall clock; return whether
    bytes grew since the last reap. Any growth resets the clock, so a download
    that is still transferring can never be mistaken for a stall."""
    cur = bytes_on_disk(entry) if entry is not None else 0
    if cur > job.last_bytes:
        job.last_bytes = cur
        job.last_progress_at = now
        return True
    return False


def _terminate(job: _Job) -> None:
    """Best-effort stop of a worker process: terminate, then kill if it lingers.
    Must be called WITHOUT holding ``_lock`` — it can wait on the process."""
    try:
        if job.proc.poll() is None:
            job.proc.terminate()
            try:
                job.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                job.proc.kill()
                try:
                    job.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                    log.error("Download worker for %s would not die.", job.key)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("Error terminating worker for %s: %s", job.key, e)


def _drop_partial_sentinel(entry: ModelEntry) -> None:
    """Keep installed-detection honest after a failed or cancelled attempt.

    ``start_download`` refuses to run when the model is installed, so any
    sentinel on disk when an attempt ends non-successfully was written BY that
    attempt — and for snapshot repos hf fetches small files (configs, exactly
    the sentinels of ECAPA/GLiNER) early, long before the weights. Left in
    place it would make a half-downloaded model read "installed" and block the
    retry. Removing just the sentinel costs one small re-fetch on resume; the
    partial weight files stay for hf's resume logic.
    """
    path = os.path.join(model_dir(entry), entry.sentinel_rel)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as e:  # pragma: no cover - defensive
        log.warning("Could not remove partial sentinel %s: %s", path, e)


def reap() -> None:
    """Fold finished, dead, or stalled workers into terminal states (called
    before every read), then pump the queue — a reaped job frees a slot.

    A job leaves ``downloading`` three ways, and only a clean success (or an
    explicit cancel) is silent; every other exit records an actionable error:

      1. the worker exited on its own — ``poll()`` returns an exit code;
      2. the worker is gone even though ``poll()`` never saw the exit — the PID
         liveness backstop (``os.kill(pid, 0)``) catches it;
      3. the worker is wedged — no new bytes for ``STALL_TIMEOUT_SEC`` — so it is
         terminated and reported rather than left at a stuck percent forever.

    This is what turns a dead or hung download into a visible error the user can
    retry, instead of an eternal "downloading" the UI can only spin on.
    """
    failed: list[str] = []
    to_terminate: list[_Job] = []
    now = time.time()
    with _lock:
        for key in list(_jobs):
            job = _jobs[key]
            rc = job.proc.poll()

            if rc is not None:
                # (1) The worker exited on its own.
                del _jobs[key]
                if job.cancelled or rc == 0:
                    _errors.pop(key, None)
                    continue
                _errors[key] = _failure_message(job.log_path, rc)
                _progress_max.pop(key, None)  # run over — next run re-measures
                log.warning("Model download %s failed (rc=%s).", key, rc)
                failed.append(key)
                continue

            # poll() reports the worker as still running. It can still be a job
            # the user must be freed from.
            entry = get_entry(key)
            if entry is not None and is_installed(entry):
                # Finished (sentinel present); the exit just hasn't been observed
                # yet. Leave it for the rc==0 sweep on the next poll.
                continue

            grew = _note_byte_progress(job, entry, now)
            alive = _pid_alive(job.pid)
            stalled = (not grew) and (now - job.last_progress_at) >= STALL_TIMEOUT_SEC
            if alive and not stalled:
                continue  # genuinely downloading (or still within the stall grace)

            # (2) gone without an observed exit, or (3) wedged with no progress.
            del _jobs[key]
            _progress_max.pop(key, None)
            if alive:  # (3) wedged: stop it (outside the lock) and report a stall
                to_terminate.append(job)
                _errors[key] = (
                    f"Download stalled: no progress for over {int(STALL_TIMEOUT_SEC)}s. "
                    "The worker was stopped; use Retry to resume."
                )
                log.warning("Model download %s stalled; terminating worker.", key)
            else:  # (2) worker vanished
                _errors[key] = _failure_message(job.log_path, None)
                log.warning("Model download %s worker vanished (pid=%s).", key, job.pid)
            failed.append(key)

    for job in to_terminate:
        _terminate(job)
    for key in failed:
        entry = get_entry(key)
        if entry is not None:
            _drop_partial_sentinel(entry)
    _pump()


def _pump() -> None:
    """Start queued downloads while slots are free, in FIFO order.

    Runs after every ``reap()`` (so the 1s status poll refills slots) and from
    ``cancel()`` (a terminated job frees its slot immediately). Keys whose
    entry vanished (config edit) or that turned out installed are dropped.
    """
    with _lock:
        while len(_jobs) < MAX_CONCURRENT and _queue:
            key = _queue.pop(0)
            entry = get_entry(key)
            if entry is None or is_installed(entry):
                continue
            log_path = _log_path(key)
            proc = _spawn_worker(key, log_path)
            _jobs[key] = _Job(key, proc, log_path)
            _errors.pop(key, None)
            # Dequeue-start is a run boundary, exactly like start_download's
            # direct start: the high-water progress mark resets so the new run
            # re-derives its fraction from real bytes.
            _progress_max.pop(key, None)
            log.info("Model download dequeued and started: %s (%s).", key, entry.repo_id)


def queue_position(key: str) -> int | None:
    """1-based position in the download queue, or None when not queued."""
    with _lock:
        try:
            return _queue.index(key) + 1
        except ValueError:
            return None


def state_of(entry: ModelEntry) -> tuple[str, str | None]:
    """(state, error) — downloading > queued > installed > error > absent."""
    with _lock:
        if entry.key in _jobs:
            return "downloading", None
        if entry.key in _queue:
            return "queued", None
    if is_installed(entry):
        return "installed", None
    err = _errors.get(entry.key)
    if err:
        return "error", err
    return "absent", None


def progress_of(entry: ModelEntry, state: str | None = None) -> float | None:
    """THE download-progress fraction — the one number every surface shows.

    Settings > Models, the pre-recording gate, and the local-chat loading
    banner must all render this, never their own bytes/size arithmetic.
    Semantics:
      - ``1.0`` once the sentinel says installed;
      - otherwise ``bytes_on_disk / estimate`` capped at ``0.99`` (bytes on
        disk include hf bookkeeping like ``.cache``/``.incomplete`` files, so
        the raw ratio can overshoot before the download is actually done);
      - monotonic per run via the high-water mark (the estimate can be revised
        upward mid-run, which would otherwise walk the percent backwards);
      - ``None`` while no size estimate exists yet.
    """
    if state is None:
        state, _ = state_of(entry)
    if state == "installed":
        with _lock:
            _progress_max.pop(entry.key, None)
        return 1.0
    if state == "queued":
        # No run yet, so no progress. The high-water mark resets at
        # dequeue-start (see _pump), the same run boundary as a direct start.
        return None
    est = sizes.estimate(entry)
    if not est or est <= 0:
        return None
    frac = min(0.99, bytes_on_disk(entry) / est)
    with _lock:
        frac = max(frac, _progress_max.get(entry.key, 0.0))
        _progress_max[entry.key] = frac
    return round(frac, 4)


def start_download(entry: ModelEntry) -> str:
    """Start (or queue) a download. Returns what happened:

      - ``"started"``            — a worker is running now
      - ``"queued"``             — all slots busy; the key joined the FIFO queue
      - ``"already_downloading"``/``"already_queued"`` — benign duplicate, no-op

    Only "already installed" is a Conflict — a duplicate request for a running
    or queued download is exactly what the requester wanted, so it succeeds.
    """
    reap()
    if is_installed(entry):
        raise Conflict("This model is already installed.")
    log_path = _log_path(entry.key)
    with _lock:
        if entry.key in _jobs:
            return "already_downloading"
        if entry.key in _queue:
            return "already_queued"
        if len(_jobs) >= MAX_CONCURRENT:
            _queue.append(entry.key)
            log.info("Model download queued: %s (position %d).", entry.key, len(_queue))
            return "queued"
        proc = _spawn_worker(entry.key, log_path)
        _jobs[entry.key] = _Job(entry.key, proc, log_path)
        _errors.pop(entry.key, None)
        # New run: drop the previous run's high-water mark. Partial bytes on
        # disk immediately re-derive an equal-or-honest fraction from real data.
        _progress_max.pop(entry.key, None)
    log.info("Model download started: %s (%s).", entry.key, entry.repo_id)
    return "started"


def _dequeue_locked(key: str) -> bool:
    """Remove a key from the queue (caller holds ``_lock``). Partials stay —
    a queued model never started, so there is nothing new on disk to unwind."""
    if key not in _queue:
        return False
    _queue.remove(key)
    _errors.pop(key, None)
    _progress_max.pop(key, None)
    return True


def cancel(entry: ModelEntry) -> str:
    """Cancel a running download (terminate the worker; partial files stay on
    disk so a retry resumes) or a queued one (just dequeue it). Returns
    ``"cancelled"`` or ``"dequeued"``."""
    reap()
    with _lock:
        if _dequeue_locked(entry.key):
            log.info("Model download dequeued: %s.", entry.key)
            return "dequeued"
        job = _jobs.pop(entry.key, None)
    if job is None:
        raise Conflict("This model is not downloading or queued.")
    job.cancelled = True
    _terminate(job)
    # rc == 0 means the download actually completed before the terminate
    # landed — then the sentinel is legitimate and stays.
    if job.proc.poll() != 0:
        _drop_partial_sentinel(entry)
    _errors.pop(entry.key, None)
    with _lock:
        _progress_max.pop(entry.key, None)
    log.info("Model download cancelled: %s.", entry.key)
    _pump()  # the terminated job freed a slot — start the next queued download
    return "cancelled"


def _stop_llama_if_serving(key: str) -> bool:
    """Stop the model server when it is serving exactly this model (either
    engine). Best-effort: a failure here must not block the delete (the file
    unlink still works — the running server keeps its mapped copy until it
    exits)."""
    try:
        from server.local import serving

        if serving.resident_key() == key:
            log.info("Stopping the model server before deleting %s.", key)
            serving.stop()
            return True
    except Exception as e:
        log.warning("Could not stop the model server before deleting %s: %s", key, e)
    return False


def delete(entry: ModelEntry) -> dict:
    """Remove the model from disk. Refused while its download is running; a
    QUEUED model is dequeued first, then whatever is on disk is removed."""
    reap()
    with _lock:
        if entry.key in _jobs:
            raise Conflict("This model is downloading; cancel the download first.")
        dequeued = _dequeue_locked(entry.key)
    if dequeued:
        log.info("Model download dequeued before delete: %s.", entry.key)
    root = model_dir(entry)
    if not os.path.exists(root):
        if dequeued:
            # Honoring the delete meant dequeuing; with nothing on disk that
            # was the whole job — report it as done rather than a conflict.
            return {"deleted": False, "dequeued": True, "stopped_llama_server": False}
        raise Conflict("Nothing to delete: this model is not on disk.")

    stopped = False
    if entry.group == GROUP_LOCAL_CHAT:
        stopped = _stop_llama_if_serving(entry.key)
        # A registry entry normally owns its directory, but two config entries
        # MAY point different filenames at one dir — then only our file goes.
        shared = any(
            other.key != entry.key and other.dir_name == entry.dir_name for other in entries()
        )
        if shared and entry.gguf_filename:
            target = os.path.join(root, entry.gguf_filename)
            if os.path.exists(target):
                os.remove(target)
        elif shared:
            # A whole-directory model (MLX snapshot) sharing its dir with
            # another entry: rmtree here would silently destroy the other
            # model's weights. Browser installs namespace MLX dirs so this
            # only fires for hand-written config collisions — refuse loudly.
            raise Conflict(
                "This model shares its folder with another entry; delete the "
                "other entry first or separate their `dir` values in config."
            )
        else:
            shutil.rmtree(root)
    else:
        shutil.rmtree(root)

    with _lock:
        _errors.pop(entry.key, None)
        _progress_max.pop(entry.key, None)
    log.info("Model deleted: %s (%s).", entry.key, root)
    return {"deleted": True, "dequeued": dequeued, "stopped_llama_server": stopped}
