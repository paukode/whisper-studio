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

Recommended vs shipped: the app ships NO local chat models by default — a fresh
install has an empty on-device catalog until the user downloads one from Settings
> Models > Discover. ``RECOMMENDED_LOCAL_MODELS`` below is a curated set the
Discover tab surfaces for one-click install; a recommended model only enters the
effective catalog once its weights are on disk (so it shows up after download,
and a model downloaded by a prior release is not orphaned). Anything the user
adds/installs lands in ``chat_models`` and is always present, even mid-download.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("whisper-studio")

# Fields a config entry must carry to be loadable on its own. An is_local entry
# missing any of them is only usable if a recommended entry of the same key
# fills the gap.
_REQUIRED = ("repo_id", "filename", "dir")

# Curated on-device models the Discover tab offers for one-click install. These
# are NOT shipped into the catalog — a recommended model only appears (and only
# becomes loadable) once its weights are on disk. `id` is a sentinel that never
# reaches Bedrock (the router branches on the "local:" prefix before any AWS
# call). 32K ctx default: the FULL tool pool ("Tools: All") renders a ~17.2K-token
# prompt, so a 16K window rejects the first tool turn; 32K loads on an M3/18GB
# with one slot and llama.cpp's windowed SWA cache. The chat-input slider still
# changes it live per model.
RECOMMENDED_LOCAL_MODELS: dict[str, dict] = {
    "local_gemma": {
        "id": "local:gemma-4-12b-it-qat-q4_0",
        "label": "Gemma 4 12B (Local)",
        "repo_id": "google/gemma-4-12B-it-qat-q4_0-gguf",
        "filename": "gemma-4-12b-it-qat-q4_0.gguf",
        "dir": "gemma-4-12b-it-qat-q4_0",
        "ctx": 32768,
        "supports_thinking": True,
        "supports_tools": True,
        # Exact GGUF size from the HF repo listing — lets Discover warn before
        # download on a machine too small for this quant, same as a searched one.
        "size_bytes": 6975879296,
    },
    "local_gemma_coder": {
        "id": "local:gemma-4-12b-coder",
        "label": "Gemma 4 Coder (Local)",
        "repo_id": "yuxinlu1/gemma-4-12B-coder-fable5-composer2.5-v1-GGUF",
        "filename": "gemma4-coding-Q4_K_M.gguf",
        "dir": "gemma-4-12b-coder",
        "ctx": 32768,
        "supports_thinking": True,
        "supports_tools": True,
        "size_bytes": 7381381664,
    },
    "local_qwen35_9b": {
        "id": "local:qwen3.5-9b",
        "label": "Qwen3.5 9B (Local)",
        "repo_id": "unsloth/Qwen3.5-9B-GGUF",
        "filename": "Qwen3.5-9B-Q4_K_M.gguf",
        "dir": "qwen3.5-9b",
        "ctx": 32768,
        "supports_thinking": True,
        "supports_tools": True,
        "size_bytes": 5680522464,
    },
    "local_ministral3_14b": {
        "id": "local:ministral3-14",
        "label": "Ministral 3 14B (Local)",
        "repo_id": "unsloth/Ministral-3-14B-Reasoning-2512-GGUF",
        "filename": "Ministral-3-14B-Reasoning-2512-UD-Q4_K_XL.gguf",
        "dir": "ministral3-14",
        "ctx": 32768,
        "supports_thinking": True,
        "supports_tools": True,
        "size_bytes": 8366206144,
    },
}

# Applied to a config model that omits `ctx`. Matches the recommended defaults for
# the same reason: the full tool pool needs more than 16K.
_DEFAULT_CTX = 32768


def _config_local_models() -> dict[str, dict]:
    """Local models declared in config's ``chat_models`` (is_local entries).

    Reads the normalized merge of the SYSTEM and USER layers WITHOUT the
    ``chat_models_disabled`` hide-list: disabling a model only hides it from the
    composer picker — its weights must stay downloadable/deletable here, or a
    disabled model's gigabytes would become unmanageable (and, for user-added
    models, invisible). Never raises: a broken config must not take the local
    runtime down.
    """
    try:
        from server.infrastructure.config import unfiltered_chat_models

        # Model ids live in the PARALLEL ``ids`` map, not in meta — reading
        # them from meta yields None and silently invents "local:<key>", which
        # then overwrites a built-in's real sentinel and breaks key_for_id().
        ids, meta = unfiltered_chat_models()
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


def _recommended_downloaded_on_disk(entry: dict) -> bool:
    """Whether a recommended model's weights are already present. Checks the GGUF
    path directly (models_root/dir/filename) rather than via runtime.is_downloaded,
    which would recurse through local_models() (this function)."""
    from server.infrastructure.paths import models_root

    d, fn = entry.get("dir"), entry.get("filename")
    if not d or not fn:
        return False
    return os.path.exists(os.path.join(models_root(), d, fn))


def recommended_models() -> dict[str, dict]:
    """The curated Discover catalog with a ``downloaded`` flag per entry. Not
    filtered by anything — the Discover tab shows every recommendation."""
    return {
        key: {**entry, "downloaded": _recommended_downloaded_on_disk(entry)}
        for key, entry in RECOMMENDED_LOCAL_MODELS.items()
    }


def local_models() -> dict[str, dict]:
    """The effective on-device catalog: recommended models that are DOWNLOADED,
    merged with the user's config ``is_local`` entries (config winning per key).

    A fresh install ships nothing local, so this is empty until the user
    downloads a model from Discover — which writes a config entry (always
    present, so an in-progress download shows immediately) and, once complete,
    also satisfies the on-disk check. A recommended model left on disk by a prior
    release still appears here even without a config entry, so its weights stay
    manageable instead of being orphaned.

    Entries that still lack a required weight field after the merge are dropped
    with a warning — a half-declared model would otherwise fail later at load
    time with a much less obvious error.
    """
    merged: dict[str, dict] = {
        key: dict(entry)
        for key, entry in RECOMMENDED_LOCAL_MODELS.items()
        if _recommended_downloaded_on_disk(entry)
    }
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
