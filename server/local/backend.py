"""Which on-device backend serves local turns.

Two backends exist:

``llama_server`` (preferred)
    The upstream ``llama-server`` subprocess over its OpenAI-compatible API.
    Upstream owns the tool-call and reasoning parsers for every family it
    supports, so a GGUF added to config gets tools + thinking with no per-model
    code here. Also supports parallel tool calls.

``in_process`` (fallback)
    llama-cpp-python in this process. Kept because it needs no external binary,
    but its tool-call and thought parsing are hardcoded to Gemma's markers, so a
    non-Gemma model silently loses tools and thinking there.

Resolution order: ``WHISPER_LOCAL_BACKEND`` env → ``local_backend`` in config →
auto. Auto picks llama-server when a new-enough binary is present, else falls
back. The choice is resolved per turn (cheap: a PATH lookup plus a cached build
probe), so installing the binary takes effect without a restart.
"""

from __future__ import annotations

import logging

log = logging.getLogger("whisper-studio")

LLAMA_SERVER = "llama_server"
IN_PROCESS = "in_process"
AUTO = "auto"

_VALID = (LLAMA_SERVER, IN_PROCESS, AUTO)

# Availability is cached because probing the build shells out. Keyed by the
# binary path so installing/upgrading (which changes or adds the path) re-probes.
_avail_cache: dict[str | None, bool] = {}


def _configured() -> str:
    import os

    raw = os.environ.get("WHISPER_LOCAL_BACKEND")
    if not raw:
        try:
            from server.infrastructure import config as cfg

            raw = cfg.get("local_backend", AUTO)
        except Exception:  # pragma: no cover - defensive
            raw = AUTO
    choice = str(raw or AUTO).strip().lower()
    if choice not in _VALID:
        log.warning(
            "Unknown local_backend %r; expected one of %s. Falling back to %s.",
            raw,
            ", ".join(_VALID),
            AUTO,
        )
        return AUTO
    return choice


def llama_server_available() -> bool:
    """True when a new-enough ``llama-server`` is installed."""
    from server.local import llama_server

    path = llama_server.binary_path()
    if path in _avail_cache:
        return _avail_cache[path]
    ok = False
    if path:
        try:
            llama_server.ensure_available()
            ok = True
        except llama_server.LlamaServerUnavailable as e:
            log.info("llama-server not usable: %s", e)
    _avail_cache[path] = ok
    return ok


def resolve() -> str:
    """The backend to use for this turn: ``llama_server`` or ``in_process``."""
    choice = _configured()
    if choice == LLAMA_SERVER:
        # Explicit request: honor it even if the probe is unsure, so a user can
        # force it. ensure_serving raises an actionable error if truly missing.
        return LLAMA_SERVER
    if choice == IN_PROCESS:
        return IN_PROCESS
    return LLAMA_SERVER if llama_server_available() else IN_PROCESS


def use_llama_server() -> bool:
    return resolve() == LLAMA_SERVER


def reset_cache() -> None:
    """Forget cached availability (tests, and after an install)."""
    _avail_cache.clear()
