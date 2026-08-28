"""Lifecycle for the ``mlx_lm server`` subprocess that serves MLX-format models.

The second on-device engine beside llama-server (see llama_server.py): same
one-resident-model policy, same OpenAI-compatible ``/v1/chat/completions``
surface, so the LocalAdapter and SSE parser are shared unchanged. Differences
that matter here:

  * the "binary" is ``python -m mlx_lm server`` inside our own interpreter, so
    availability is an import check, not a PATH lookup;
  * weights are a snapshot DIRECTORY (safetensors + config.json), not one file;
  * the server loads weights lazily on the first request, so ``ensure_serving``
    fires a one-token warmup — otherwise "loaded" would lie until the first
    chat turn paid the multi-GB load;
  * there is no ``--ctx-size``: the context window is whatever the model's
    config carries. The requested n_ctx is recorded only so the context meter
    has a number to show;
  * the request ``model`` field must be ``default_model`` — mlx_lm maps that
    sentinel to the ``--model`` path; any other name is treated as a NEW model
    to load (see serving.wire_model).

Engine choice per model lives in the registry (``engine: "mlx"``); dispatch
between the two engines lives in server/local/serving.py.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time

log = logging.getLogger("whisper-studio")

_STARTUP_TIMEOUT = float(os.environ.get("WHISPER_MLX_SERVER_TIMEOUT", "600"))
_HOST = "127.0.0.1"

_lock = threading.Lock()
# Serializes the whole (re)start sequence, same rationale as llama_server:
# concurrent loads of one model must coalesce instead of killing each other.
_load_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_state: dict = {"key": None, "port": None, "n_ctx": None}
_log_path: str | None = None

# The model name mlx_lm maps to its --model path. Any other name would be
# loaded as a new repo/path.
WIRE_MODEL = "default_model"


class MlxServerUnavailable(RuntimeError):
    """mlx-lm is not importable — carries the actionable fix."""


def is_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("mlx_lm") is not None


def ensure_available() -> None:
    if not is_available():
        raise MlxServerUnavailable(
            "mlx-lm is not installed, so MLX-format models cannot run. "
            "Install it with 'venv/bin/pip install mlx-lm' or rerun "
            "'bash setup.sh --local'."
        )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((_HOST, 0))
        return int(s.getsockname()[1])


def is_running() -> bool:
    with _lock:
        return _proc is not None and _proc.poll() is None


def resident_key() -> str | None:
    with _lock:
        return _state["key"] if _proc is not None and _proc.poll() is None else None


def resident_n_ctx() -> int | None:
    """The context size recorded at start, or None. Informational only: mlx_lm
    has no --ctx-size, so this is the registry/slider number the meter shows,
    not an enforced window."""
    with _lock:
        if _proc is None or _proc.poll() is not None:
            return None
        return _state["n_ctx"]


def base_url() -> str | None:
    with _lock:
        if _proc is None or _proc.poll() is not None or not _state["port"]:
            return None
        return f"http://{_HOST}:{_state['port']}"


def log_tail(lines: int = 40) -> str:
    if not _log_path or not os.path.exists(_log_path):
        return ""
    try:
        with open(_log_path, errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except Exception:  # pragma: no cover - defensive
        return ""


def stop() -> None:
    """Terminate the subprocess if running. Safe to call when it isn't."""
    global _proc
    with _lock:
        proc, _proc = _proc, None
        _state.update(key=None, port=None, n_ctx=None)
    if proc is None or proc.poll() is not None:
        return
    log.info("Stopping mlx_lm server (pid %s).", proc.pid)
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        log.warning("mlx_lm server did not exit on SIGTERM; killing.")
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            log.error("mlx_lm server pid %s would not die.", proc.pid)


def _wait_healthy(port: int, proc: subprocess.Popen, deadline: float) -> bool:
    import urllib.error
    import urllib.request

    url = f"http://{_HOST}:{port}/health"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError, TimeoutError):
            pass
        time.sleep(0.5)
    return False


def _warmup(port: int, deadline: float) -> str | None:
    """Force the lazy weight load with a one-token request. Returns an error
    string on failure, None on success.

    /health answers before any weights are read, so without this the model
    would report "loaded" instantly and the first chat turn would silently pay
    the multi-GB load (or surface its failure mid-conversation).
    """
    import httpx

    timeout = max(30.0, deadline - time.monotonic())
    try:
        r = httpx.post(
            f"http://{_HOST}:{port}/v1/chat/completions",
            json={
                "model": WIRE_MODEL,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
                "stream": False,
            },
            timeout=timeout,
        )
        if r.status_code != 200:
            return f"warmup returned {r.status_code}: {r.text[:300]}"
        return None
    except Exception as e:
        return f"warmup failed: {e}"


def ensure_serving(key: str, n_ctx: int | None = None) -> str:
    """Guarantee ``key`` is being served and return its base URL.

    Blocking — the warmup loads gigabytes of weights on a cold start, so call
    it off the event loop. Raises MlxServerUnavailable with the fix, or
    RuntimeError with the subprocess log tail when the model fails to load.
    """
    global _proc, _log_path

    from server.local.registry import local_models
    from server.local.runtime import MODELS_DIR, ensure_downloaded, requested_n_ctx

    models = local_models()
    if key not in models:
        raise RuntimeError(f"Unknown local model: {key}")
    meta = models[key]
    target_ctx = int(n_ctx or requested_n_ctx() or meta.get("ctx") or 32768)

    with _lock:
        if _proc is not None and _proc.poll() is None and _state["key"] == key:
            # Unlike llama-server, a context-size change needs no restart:
            # there is no --ctx-size flag, so just record the new number.
            _state["n_ctx"] = target_ctx
            return f"http://{_HOST}:{_state['port']}"

    with _load_lock:
        with _lock:
            if _proc is not None and _proc.poll() is None and _state["key"] == key:
                _state["n_ctx"] = target_ctx
                return f"http://{_HOST}:{_state['port']}"

        ensure_available()
        # Fetch weights before spawning, same as llama_server: a missing
        # snapshot would otherwise surface as an opaque warmup failure.
        model_dir = ensure_downloaded(key)

        stop()  # one resident model at a time (within this engine)

        port = _free_port()
        os.makedirs(os.path.join(MODELS_DIR, ".logs"), exist_ok=True)
        log_path = os.path.join(MODELS_DIR, ".logs", f"mlx-server-{key}.log")
        cmd = [
            sys.executable,
            "-m",
            "mlx_lm",
            "server",
            "--model",
            model_dir,
            "--host",
            _HOST,
            "--port",
            str(port),
        ]
        extra = os.environ.get("WHISPER_MLX_SERVER_ARGS", "").split()
        cmd.extend(extra)

        log.info("Starting mlx_lm server for %s on port %d; log -> %s", key, port, log_path)
        logfile = open(log_path, "w")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=logfile,
                stderr=subprocess.STDOUT,
                # Own process group so a signal aimed at our shell/parent
                # doesn't take the model server down mid-turn.
                start_new_session=True,
            )
        except Exception as e:
            logfile.close()
            raise RuntimeError(f"Could not start mlx_lm server: {e}") from e

        with _lock:
            _proc = proc
            _log_path = log_path
            _state.update(key=key, port=port, n_ctx=target_ctx)

        deadline = time.monotonic() + _STARTUP_TIMEOUT
        if not _wait_healthy(port, proc, deadline):
            tail = log_tail()
            stop()
            raise RuntimeError(
                f"mlx_lm server failed to become ready for {key}. Last log lines:\n{tail}"
            )
        warmup_error = _warmup(port, deadline)
        if warmup_error:
            tail = log_tail()
            stop()
            raise RuntimeError(
                f"mlx_lm server could not load {key} ({warmup_error}). Last log lines:\n{tail}"
            )
        log.info("mlx_lm server ready for %s on port %d.", key, port)
        return f"http://{_HOST}:{port}"


def reap_orphans() -> int:
    """Kill leftover mlx_lm servers serving OUR models. Returns the count.

    Same rationale and narrow matching as llama_server.reap_orphans: only
    processes whose --model path is inside our models directory are touched, so
    an mlx_lm server the user runs for their own purposes is never affected.
    """
    from server.local.runtime import MODELS_DIR

    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,args="], capture_output=True, text=True, timeout=20
        ).stdout
    except Exception as e:  # pragma: no cover - environment dependent
        log.debug("Could not scan for mlx_lm server orphans: %s", e)
        return 0

    markers = {MODELS_DIR, os.path.realpath(MODELS_DIR)}
    killed = 0
    for line in out.splitlines():
        line = line.strip()
        if not line or "mlx_lm" not in line:
            continue
        pid_str, _, args = line.partition(" ")
        if not pid_str.isdigit() or not any(m in args for m in markers):
            continue
        pid = int(pid_str)
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
            log.info("Reaped orphaned mlx_lm server (pid %d) from a previous run.", pid)
        except (ProcessLookupError, PermissionError) as e:
            log.debug("Could not reap mlx_lm server pid %d: %s", pid, e)
    return killed


def status() -> dict:
    """Snapshot for /api diagnostics."""
    return {
        "installed": is_available(),
        "running": is_running(),
        "model": resident_key(),
        "base_url": base_url(),
        "n_ctx": _state.get("n_ctx"),
    }
