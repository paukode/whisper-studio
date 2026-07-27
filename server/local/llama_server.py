"""Lifecycle for the ``llama-server`` subprocess that serves on-device models.

Why a subprocess instead of the in-process llama-cpp-python bindings: upstream
llama.cpp's server owns the tool-calling and reasoning parsers for every model
family it supports, so a new GGUF dropped into config gets tool calls and a
thinking channel WITHOUT us hand-writing a per-family parser. The bindings expose
no such parser (only Functionary/ChatML handlers that would replace the model's
own template), which is why the previous path was hardcoded to Gemma's markers.

What this module owns:
  * finding the binary (PATH, then Homebrew)
  * starting it against one model at a chosen context size, and waiting for
    /health
  * one resident model at a time — switching models stops the old process first,
    matching the memory-constrained on-device build (e.g. M3/18GB)
  * stopping it cleanly on server shutdown so no orphan holds the port or RAM

It deliberately does NOT know about chat, tools or SSE — server/local/
server_stream.py speaks HTTP to whatever this has running.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
import time

log = logging.getLogger("whisper-studio")

# gemma4 (and any newer architecture) needs a recent llama.cpp. Homebrew's 6940
# loads gemma3 but fails gemma4 with "unknown model architecture", so refusing
# early with the upgrade command beats a confusing load error. Bump as needed.
MIN_BUILD = 10090

# Where we look for the binary when it is not on PATH.
_FALLBACK_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")

_STARTUP_TIMEOUT = float(os.environ.get("WHISPER_LLAMA_SERVER_TIMEOUT", "600"))
# One-shot (non-chat) generations: a long prompt on a 12B can prefill for a
# couple of minutes before the first token, so this is generous by design. The
# callers are all background work that must not wedge on a slow decode either.
_ONESHOT_TIMEOUT = float(os.environ.get("WHISPER_LLAMA_ONESHOT_TIMEOUT", "600"))
_HOST = "127.0.0.1"

_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_state: dict = {"key": None, "port": None, "n_ctx": None}
_log_path: str | None = None


class LlamaServerUnavailable(RuntimeError):
    """The binary is missing or too old — carries the actionable fix."""


def binary_path() -> str | None:
    """Absolute path to ``llama-server``, or None if it isn't installed."""
    found = shutil.which("llama-server")
    if found:
        return found
    for d in _FALLBACK_DIRS:
        candidate = os.path.join(d, "llama-server")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def installed_build() -> int | None:
    """The installed build number, or None if it can't be determined.

    llama-server prints its build to stderr and also loads a GPU backend on
    ``--version``, so this is best-effort and short-timeout: an unknown build is
    treated as "probably fine" rather than blocking startup.
    """
    exe = binary_path()
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60)
        blob = f"{out.stderr}\n{out.stdout}"
    except Exception as e:  # pragma: no cover - environment dependent
        log.debug("llama-server --version failed: %s", e)
        return None
    import re

    # Current builds print "version: 10090 (7347430f4)"; older/other builds have
    # used "build: 3901" and a bare "b3901" tag. Try all three.
    m = (
        re.search(r"^\s*version:\s*(\d+)", blob, re.I | re.M)
        or re.search(r"build[:\s]+(\d+)", blob, re.I)
        or re.search(r"\bb(\d{4,})\b", blob)
    )
    return int(m.group(1)) if m else None


def ensure_available() -> str:
    """Return the binary path, or raise with the exact command to fix it."""
    exe = binary_path()
    if not exe:
        raise LlamaServerUnavailable(
            "llama-server is not installed, so on-device models cannot run. "
            "Install it with 'brew install llama.cpp' (macOS) or run "
            "'bash setup.sh --local', which does it for you."
        )
    build = installed_build()
    if build is not None and build < MIN_BUILD:
        raise LlamaServerUnavailable(
            f"llama-server build {build} is too old for current model architectures "
            f"(need >= {MIN_BUILD}; older builds fail with 'unknown model "
            "architecture'). Upgrade with 'brew upgrade llama.cpp'."
        )
    return exe


def _free_port() -> int:
    """An ephemeral free port. Bound and released, so there is a small race with
    other processes; the health wait below surfaces a loser immediately."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((_HOST, 0))
        return int(s.getsockname()[1])


def is_running() -> bool:
    with _lock:
        return _proc is not None and _proc.poll() is None


def resident_key() -> str | None:
    """Key of the model currently being served, or None."""
    with _lock:
        return _state["key"] if _proc is not None and _proc.poll() is None else None


def resident_n_ctx() -> int | None:
    """Context size the running process was started with, or None.

    This is the TRUE window for an on-device turn — the ``--ctx-size`` the
    subprocess was launched with — so the context meter must read it here rather
    than from the cloud ``context_windows`` config map, which knows nothing about
    local models.
    """
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
    """Last lines of the subprocess log — used to explain a failed start."""
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
    log.info("Stopping llama-server (pid %s).", proc.pid)
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        log.warning("llama-server did not exit on SIGTERM; killing.")
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            log.error("llama-server pid %s would not die.", proc.pid)


def _wait_healthy(port: int, proc: subprocess.Popen, deadline: float) -> bool:
    """Poll /health until the server answers, the process dies, or we time out."""
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
        time.sleep(1.0)
    return False


def ensure_serving(key: str, n_ctx: int | None = None) -> str:
    """Guarantee ``key`` is being served and return its base URL.

    Reuses the running process when it already serves this key at this context
    size; otherwise stops it and starts a new one (only one model is resident).
    Blocking — a cold start loads gigabytes of weights, so call it off the event
    loop. Raises LlamaServerUnavailable with the fix, or RuntimeError with the
    subprocess log tail when the model itself fails to load.
    """
    global _proc, _log_path

    from server.local.registry import local_models
    from server.local.runtime import MODELS_DIR, ensure_downloaded, requested_n_ctx

    models = local_models()
    if key not in models:
        raise RuntimeError(f"Unknown local model: {key}")
    meta = models[key]
    # Explicit request > the user's last slider choice (sticky, shared with the
    # in-process runtime) > the model default. Without the sticky tier a lazy
    # chat-turn start would restart the server back down at 16K even though the
    # badge says 32K/64K.
    target_ctx = int(n_ctx or requested_n_ctx() or meta.get("ctx") or 16384)

    with _lock:
        if (
            _proc is not None
            and _proc.poll() is None
            and _state["key"] == key
            and _state["n_ctx"] == target_ctx
        ):
            return f"http://{_HOST}:{_state['port']}"

    exe = ensure_available()
    # Fetch weights before spawning: a missing GGUF would otherwise surface as an
    # opaque subprocess exit, and the download can take a long time.
    model_path = ensure_downloaded(key)

    stop()  # one resident model at a time

    port = _free_port()
    os.makedirs(os.path.join(MODELS_DIR, ".logs"), exist_ok=True)
    log_path = os.path.join(MODELS_DIR, ".logs", f"llama-server-{key}.log")
    cmd = [
        exe,
        "--model",
        model_path,
        # --jinja is what activates the model's own chat template AND upstream's
        # per-family tool-call parser. Without it, tools are not parsed at all.
        "--jinja",
        "--host",
        _HOST,
        "--port",
        str(port),
        "--ctx-size",
        str(target_ctx),
        # ONE slot. The default is 4, and each slot gets its own KV cache, so the
        # default quadruples KV memory for a single-user desktop app — enough to
        # OOM Metal on an 18GB machine at a large context. One request at a time
        # is also what the UI does.
        "--parallel",
        "1",
        # Reuse cached prompt prefixes across turns via KV shifting — the
        # subprocess equivalent of the in-process prefix cache, so a follow-up
        # turn doesn't re-prefill the whole system+tools prompt.
        "--cache-reuse",
        "256",
        # Extract reasoning into a separate channel instead of leaking the raw
        # thought markers into the answer.
        "--reasoning-format",
        "auto",
    ]
    extra = os.environ.get("WHISPER_LLAMA_SERVER_ARGS", "").split()
    cmd.extend(extra)

    log.info(
        "Starting llama-server for %s (n_ctx=%d) on port %d; log -> %s",
        key,
        target_ctx,
        port,
        log_path,
    )
    logfile = open(log_path, "w")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=logfile,
            stderr=subprocess.STDOUT,
            # Own process group so a signal aimed at our shell/parent doesn't
            # take the model server down mid-turn.
            start_new_session=True,
        )
    except Exception as e:
        logfile.close()
        raise RuntimeError(f"Could not start llama-server: {e}") from e

    with _lock:
        _proc = proc
        _log_path = log_path
        _state.update(key=key, port=port, n_ctx=target_ctx)

    if not _wait_healthy(port, proc, time.monotonic() + _STARTUP_TIMEOUT):
        tail = log_tail()
        stop()
        raise RuntimeError(
            f"llama-server failed to become ready for {key}. Last log lines:\n{tail}"
        )
    log.info("llama-server ready for %s on port %d.", key, port)
    return f"http://{_HOST}:{port}"


def _strip_reasoning_markers(text: str) -> str:
    """Drop a model's reasoning-channel markers (and any reasoning ahead of the
    close marker) from one-shot content.

    Observed on the non-streaming endpoint: when upstream's reasoning parser does
    not claim a model's thought channel, ``reasoning_content`` comes back empty and
    the content arrives as ``"<channel|>the real answer"`` — sometimes with the
    whole thought block ahead of it. Callers here want clean prose or JSON (session
    summaries, index extraction), so keep only what follows the LAST close marker
    and drop any stray markers. Text without markers is returned untouched.
    """
    if not text:
        return text
    for close in ("<channel|>", "</think>"):
        if close in text:
            text = text.rsplit(close, 1)[1]
    for stray in ("<|channel>thought", "<|channel>", "<think>", "<|think|>"):
        text = text.replace(stray, "")
    return text.strip()


def complete(key: str, system_prompt: str, user: str, max_tokens: int = 1500) -> str:
    """One-shot, NON-streaming generation; returns the assistant text ('' on
    failure). Blocking — starts the model server if it isn't already serving
    ``key``, so call it off the event loop.

    This is the non-chat entry point (workspace-index extraction, the on-device
    session summariser). It deliberately sends no tools: these callers want prose
    or JSON back, not a tool call.
    """
    import httpx

    try:
        url = ensure_serving(key)
    except Exception as e:
        log.warning("complete(%s): model server unavailable: %s", key, e)
        return ""
    try:
        r = httpx.post(
            f"{url}/v1/chat/completions",
            json={
                "model": key,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_tokens,
                "stream": False,
            },
            timeout=_ONESHOT_TIMEOUT,
        )
        r.raise_for_status()
        choices = (r.json() or {}).get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        return _strip_reasoning_markers(msg.get("content") or "")
    except Exception as e:
        log.warning("complete(%s) failed: %s", key, e)
        return ""


def reap_orphans() -> int:
    """Kill leftover llama-server processes serving OUR models. Returns the count.

    The lifespan shutdown hook stops the child on a graceful exit, but SIGKILL
    (force quit, OOM killer, `kill -9`) skips it — and because the child runs in
    its own session it survives, holding gigabytes and its port forever. Called
    at startup so a hard-killed run cleans up after itself.

    Matching is deliberately narrow: only processes whose ``--model`` path is
    inside our models directory. A llama-server the user runs for their own
    purposes is never touched.
    """
    from server.local.runtime import MODELS_DIR

    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,args="], capture_output=True, text=True, timeout=20
        ).stdout
    except Exception as e:  # pragma: no cover - environment dependent
        log.debug("Could not scan for llama-server orphans: %s", e)
        return 0

    marker = os.path.realpath(MODELS_DIR)
    killed = 0
    for line in out.splitlines():
        line = line.strip()
        if not line or "llama-server" not in line:
            continue
        pid_str, _, args = line.partition(" ")
        if not pid_str.isdigit() or marker not in args:
            continue
        pid = int(pid_str)
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
            log.info("Reaped orphaned llama-server (pid %d) from a previous run.", pid)
        except (ProcessLookupError, PermissionError) as e:
            log.debug("Could not reap llama-server pid %d: %s", pid, e)
    return killed


def status() -> dict:
    """Snapshot for /api diagnostics."""
    exe = binary_path()
    return {
        "installed": bool(exe),
        "binary": exe,
        "min_build": MIN_BUILD,
        "running": is_running(),
        "model": resident_key(),
        "base_url": base_url(),
        "n_ctx": _state.get("n_ctx"),
    }
