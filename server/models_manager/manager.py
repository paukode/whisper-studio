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


class Conflict(Exception):
    """A request that is valid but impossible right now (HTTP 409)."""


class _Job:
    __slots__ = ("key", "proc", "log_path", "started_at", "cancelled")

    def __init__(self, key: str, proc: subprocess.Popen, log_path: str) -> None:
        self.key = key
        self.proc = proc
        self.log_path = log_path
        self.started_at = time.time()
        self.cancelled = False


_jobs: dict[str, _Job] = {}
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


def _log_tail(log_path: str, lines: int = 12) -> str:
    try:
        with open(log_path, errors="replace") as f:
            return "".join(f.readlines()[-lines:]).strip()
    except OSError:
        return ""


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
    """Fold finished workers into terminal states (called before every read)."""
    failed: list[str] = []
    with _lock:
        for key in list(_jobs):
            job = _jobs[key]
            rc = job.proc.poll()
            if rc is None:
                continue
            del _jobs[key]
            if job.cancelled or rc == 0:
                _errors.pop(key, None)
                continue
            tail = _log_tail(job.log_path)
            _errors[key] = tail or f"Download worker exited with code {rc}."
            _progress_max.pop(key, None)  # run over — next run re-measures
            log.warning("Model download %s failed (rc=%s).", key, rc)
            failed.append(key)
    for key in failed:
        entry = get_entry(key)
        if entry is not None:
            _drop_partial_sentinel(entry)


def state_of(entry: ModelEntry) -> tuple[str, str | None]:
    """(state, error) — downloading > installed > error > absent."""
    with _lock:
        if entry.key in _jobs:
            return "downloading", None
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
    est = sizes.estimate(entry)
    if not est or est <= 0:
        return None
    frac = min(0.99, bytes_on_disk(entry) / est)
    with _lock:
        frac = max(frac, _progress_max.get(entry.key, 0.0))
        _progress_max[entry.key] = frac
    return round(frac, 4)


def start_download(entry: ModelEntry) -> None:
    reap()
    if is_installed(entry):
        raise Conflict("This model is already installed.")
    log_path = _log_path(entry.key)
    with _lock:
        if entry.key in _jobs:
            raise Conflict("This model is already downloading.")
        if len(_jobs) >= MAX_CONCURRENT:
            raise Conflict(
                f"{MAX_CONCURRENT} downloads are already running; wait for one to finish."
            )
        proc = _spawn_worker(entry.key, log_path)
        _jobs[entry.key] = _Job(entry.key, proc, log_path)
        _errors.pop(entry.key, None)
        # New run: drop the previous run's high-water mark. Partial bytes on
        # disk immediately re-derive an equal-or-honest fraction from real data.
        _progress_max.pop(entry.key, None)
    log.info("Model download started: %s (%s).", entry.key, entry.repo_id)


def cancel(entry: ModelEntry) -> None:
    """Terminate the worker. Partial files stay on disk so a retry resumes."""
    reap()
    with _lock:
        job = _jobs.pop(entry.key, None)
    if job is None:
        raise Conflict("This model is not downloading.")
    job.cancelled = True
    if job.proc.poll() is None:
        job.proc.terminate()
        try:
            job.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            job.proc.kill()
            try:
                job.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                log.error("Download worker for %s would not die.", entry.key)
    # rc == 0 means the download actually completed before the terminate
    # landed — then the sentinel is legitimate and stays.
    if job.proc.poll() != 0:
        _drop_partial_sentinel(entry)
    _errors.pop(entry.key, None)
    with _lock:
        _progress_max.pop(entry.key, None)
    log.info("Model download cancelled: %s.", entry.key)


def _stop_llama_if_serving(key: str) -> bool:
    """Stop llama-server when it is serving exactly this model. Best-effort:
    a failure here must not block the delete (the file unlink still works —
    the running server keeps its mapped copy until it exits)."""
    try:
        from server.local import llama_server

        if llama_server.resident_key() == key:
            log.info("Stopping llama-server before deleting %s.", key)
            llama_server.stop()
            return True
    except Exception as e:
        log.warning("Could not stop llama-server before deleting %s: %s", key, e)
    return False


def delete(entry: ModelEntry) -> dict:
    """Remove the model from disk. Refused while its download is running."""
    reap()
    with _lock:
        if entry.key in _jobs:
            raise Conflict("This model is downloading; cancel the download first.")
    root = model_dir(entry)
    if not os.path.exists(root):
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
        else:
            shutil.rmtree(root)
    else:
        shutil.rmtree(root)

    with _lock:
        _errors.pop(entry.key, None)
        _progress_max.pop(entry.key, None)
    log.info("Model deleted: %s (%s).", entry.key, root)
    return {"deleted": True, "stopped_llama_server": stopped}
