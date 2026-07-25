"""The on-device model registry — built-in defaults plus anything in config.

Adding a local model is a ONE-place change: give it an entry in ``chat_models``
with ``is_local: true`` and the weight fields, and it becomes downloadable,
loadable and selectable with no code change::

    "local_llama4": {
      "id": "local:llama-4-8b",          # must start with "local:" (see runtime)
      "label": "Llama 4 8B (Local)",
      "is_local": true,
      "supports_thinking": true,
      "supports_tools": true,
      "repo_id": "unsloth/Llama-4-8B-Instruct-GGUF",
      "filename": "Llama-4-8B-Instruct-Q4_K_M.gguf",
      "dir": "llama-4-8b",
      "ctx": 32768
    }

Why the same entry as the picker metadata rather than a separate ``local_models``
section: the picker fields (``is_local``, ``supports_tools``, ``label``) and the
loader fields (``repo_id``, ``filename``, ``dir``) describe one model, and
splitting them across two config sections is how you get a model that shows in
the UI but cannot load (or vice versa).

The built-ins below stay in code so a fresh checkout with no ``config.json``
still has working on-device models. Config wins on conflict, per key, so a user
can retune just the ``ctx`` of a built-in without restating its repo.
"""

from __future__ import annotations

import logging

log = logging.getLogger("whisper-studio")

# Fields a config entry must carry to be loadable on its own. An is_local entry
# missing any of them is only usable if a built-in of the same key fills the gap.
_REQUIRED = ("repo_id", "filename", "dir")

# Shipped defaults. `id` is a sentinel that never reaches Bedrock (the router
# branches on the "local:" prefix before any AWS call).
BUILTIN_LOCAL_MODELS: dict[str, dict] = {
    "local_gemma": {
        "id": "local:gemma-4-12b-it-qat-q4_0",
        "label": "Gemma 4 12B (Local)",
        "repo_id": "google/gemma-4-12B-it-qat-q4_0-gguf",
        "filename": "gemma-4-12b-it-qat-q4_0.gguf",
        "dir": "gemma-4-12b-it-qat-q4_0",
        # Gemma 4 supports up to 262144 (256K) natively. 32K is the default
        # because the FULL tool pool ("Tools: All") renders a ~17.2K-token prompt
        # — a 16K window rejects the very first turn with exceed_context_size.
        # Measured on an M3/18GB: 32K loads fine with one slot and llama.cpp's
        # windowed SWA cache. Lower it per-model via config `ctx` on a smaller
        # machine; the chat-input slider still changes it live.
        "ctx": 32768,
        "supports_thinking": True,
        "supports_tools": True,
    },
    "local_gemma_coder": {
        "id": "local:gemma-4-12b-coder",
        "label": "Gemma 4 Coder (Local)",
        "repo_id": "yuxinlu1/gemma-4-12B-coder-fable5-composer2.5-v1-GGUF",
        "filename": "gemma4-coding-Q4_K_M.gguf",
        "dir": "gemma-4-12b-coder",
        # Same family as Gemma 4 12B above; identical context + capability flags.
        "ctx": 32768,
        "supports_thinking": True,
        "supports_tools": True,
    },
}

# Applied to a config model that omits `ctx`. Matches the built-ins for the same
# reason: the full tool pool needs more than 16K.
_DEFAULT_CTX = 32768


def _config_local_models() -> dict[str, dict]:
    """Local models declared in config's ``chat_models`` (is_local entries).

    Reads the normalized ``chat_model_meta`` map, so it sees the merged
    DEFAULTS → user → project → env result rather than raw file contents.
    Never raises: a broken config must not take the local runtime down.
    """
    try:
        from server.infrastructure.config import load_config

        cfg = load_config()
        meta = cfg.get("chat_model_meta") or {}
        # Model ids live in the PARALLEL ``chat_models`` map, not in meta — reading
        # them from meta yields None and silently invents "local:<key>", which
        # then overwrites a built-in's real sentinel and breaks key_for_id().
        ids = cfg.get("chat_models") or {}
    except Exception as e:  # pragma: no cover - defensive
        log.debug("Could not read chat model config for local registry: %s", e)
        return {}

    out: dict[str, dict] = {}
    for key, m in meta.items():
        if not isinstance(m, dict) or not m.get("is_local"):
            continue
        entry = {
            "id": ids.get(key) or f"local:{key}",
            "label": m.get("label") or key,
            "supports_thinking": bool(m.get("supports_thinking", False)),
            "supports_tools": bool(m.get("supports_tools", False)),
        }
        # Only carry weight fields the config actually set, so a built-in of the
        # same key keeps supplying the rest instead of being blanked out.
        for field in (*_REQUIRED, "ctx"):
            if m.get(field):
                entry[field] = m[field]
        out[key] = entry
    return out


def local_models() -> dict[str, dict]:
    """The effective registry: built-ins merged with config, config winning.

    Entries that still lack a required weight field after the merge are dropped
    with a warning — a half-declared model would otherwise fail later at load
    time with a much less obvious error.
    """
    merged: dict[str, dict] = {k: dict(v) for k, v in BUILTIN_LOCAL_MODELS.items()}
    for key, entry in _config_local_models().items():
        if key in merged:
            merged[key].update(entry)
        else:
            merged[key] = dict(entry)

    usable: dict[str, dict] = {}
    for key, entry in merged.items():
        missing = [f for f in _REQUIRED if not entry.get(f)]
        if missing:
            log.warning(
                "Local model %r ignored: missing %s in its chat_models entry. "
                "An is_local model needs repo_id, filename and dir to be loadable.",
                key,
                ", ".join(missing),
            )
            continue
        entry.setdefault("ctx", _DEFAULT_CTX)
        entry.setdefault("id", f"local:{key}")
        entry.setdefault("label", key)
        usable[key] = entry
    return usable


def local_ids() -> dict[str, str]:
    """``{key: sentinel id}`` for every usable local model."""
    return {k: m["id"] for k, m in local_models().items()}
