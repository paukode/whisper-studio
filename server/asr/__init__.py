"""ASR backend registry.

Backends are resolved lazily by name so an unused backend's module (and
its model stack) is never imported. The config value ``streaming`` is a
historical alias for the Parakeet backend, kept so existing config files
and the frontend enum keep working.

To remove a backend permanently: delete its module file and its BACKENDS
entry (and the alias, if any). Nothing else in the codebase imports
backend internals.
"""

from __future__ import annotations

import importlib

BACKENDS = {
    "whisper": "server.asr.whisper_backend",
    "parakeet": "server.asr.parakeet_backend",
}

_ALIASES = {
    "streaming": "parakeet",
}

DEFAULT_BACKEND = "whisper"


def resolve_name(name: str | None) -> str:
    name = (name or "").strip().lower()
    name = _ALIASES.get(name, name)
    return name if name in BACKENDS else DEFAULT_BACKEND


def get_backend(name: str | None):
    """Return the backend module for ``name`` (alias-aware, lazy import)."""
    return importlib.import_module(BACKENDS[resolve_name(name)])
