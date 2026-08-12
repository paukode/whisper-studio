"""Install / uninstall a browsed GGUF as a config-driven local chat model.

This is the P0 "config path" (design §9): a browser install writes exactly the
same ``chat_models`` dict entry a user would add by hand
(server/local/registry.py), into the USER layer (``config.user.json``), then
enqueues the download through the existing models-manager queue. Nothing
downstream is new — the registry merges the entry in, the models catalog derives
a download row from the registry, the composer picker's live refresh shows it,
and load-on-use goes through ``llama-server`` unchanged.

Two hard-won rules from the config machinery are load-bearing here:
  * the entry MUST be written into ``chat_models`` as a DICT, never into the
    derived ``chat_model_meta`` — the normalizer rebuilds meta from chat_models
    on every load and would clobber a meta-only entry;
  * the merge must READ the raw USER layer (``_load_user_config``) and write it
    back via ``save_config``, so other user entries survive. It must NOT go
    through ``PUT /api/config``, which replaces ``chat_models`` wholesale.
"""

from __future__ import annotations

import logging
import re

from server.infrastructure import config as config_mod
from server.model_browser import compat

log = logging.getLogger("whisper-studio")

# Context-window default and cap. 32768 is BOTH the floor and the default: the
# full tool pool alone renders ~12K tokens, so a smaller window rejects the
# first tool-using turn (see server/local/llama_server.py). We also cap at 32768
# by default so a 256K-capable model still loads on an 18GB Mac; a power user can
# raise ``ctx`` in config or via the chat-input slider afterwards.
DEFAULT_CTX = 32768
CTX_CAP = 32768


def _slug(text: str) -> str:
    """Lowercase, dash-separated, filesystem/id-safe slug."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _key_part(text: str) -> str:
    """Lowercase, underscore-separated fragment for a registry/config key."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def registry_key(repo_id: str, quant: str) -> str:
    """A unique, collision-proof config/registry key for (repo, quant). The
    ``local_`` prefix keeps it clearly on-device and away from the static
    catalog keys (whisper/parakeet/…) and the built-in ``local_gemma`` key."""
    return f"local_{_key_part(repo_id)}__{_key_part(quant)}"


def _model_id(repo_id: str, quant: str) -> str:
    """The picker/runtime sentinel id. MUST start with ``local:`` — cloud-path
    code branches on that prefix before any Bedrock call."""
    return f"local:{_slug(repo_id)}-{_slug(quant)}"


def _dir_name(repo_id: str) -> str:
    """The models/ subdirectory for this repo. Recognizable (mirrors the repo
    id) and filesystem-safe. Quants of one repo share the dir and differ by
    filename — the models-manager delete already handles that shared-dir case."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", repo_id.replace("/", "__"))


def resolve_ctx(requested: int | None, header_ctx: int | None) -> int:
    """The ``ctx`` to write: an explicit request wins (still floored at the 32K
    minimum); otherwise the GGUF header context, capped so it loads on this
    hardware; otherwise the 32K default."""
    if requested:
        return max(DEFAULT_CTX, int(requested))
    if header_ctx:
        return min(int(header_ctx), CTX_CAP)
    return DEFAULT_CTX


def build_entry(
    repo_id: str,
    filename: str,
    quant: str,
    arch: str | None,
    label: str | None,
    n_ctx: int | None,
    header_ctx: int | None,
) -> tuple[str, dict]:
    """(key, chat_models entry) for a browsed model — the exact hand-config shape.

    Defaults, all overridable in the config editor afterwards:
      * ``supports_tools`` True only at or above ``AGENTIC_MIN_PARAMS_B`` (or when
        the name carries no size) — the tool loop rides on llama-server
        ``--jinja``, which these instruct families support, but a model too small
        to drive it emits raw JSON into the chat instead of a parsed call;
      * ``supports_thinking`` True only for reasoning families (qwen3, QwQ,
        DeepSeek-R1), else False.
    """
    key = registry_key(repo_id, quant)
    entry = {
        "id": _model_id(repo_id, quant),
        "label": label or f"{repo_id.split('/')[-1]} {quant} (Local)",
        "is_local": True,
        "supports_thinking": compat.is_thinking_model(arch, repo_id, filename),
        "supports_tools": compat.is_agentic_capable(repo_id, filename),
        "repo_id": repo_id,
        "filename": filename,
        "dir": _dir_name(repo_id),
        "ctx": resolve_ctx(n_ctx, header_ctx),
    }
    return key, entry


def write_entry(key: str, entry: dict) -> None:
    """Merge ONE entry into the USER layer's ``chat_models`` and persist.

    Reads the raw user layer (rich shape preserved), adds/overrides just this
    key, writes back atomically. Other user entries are untouched.
    """
    raw = config_mod._load_user_config()
    chat_models = raw.get("chat_models")
    if not isinstance(chat_models, dict):
        chat_models = {}
    chat_models[key] = entry
    raw["chat_models"] = chat_models
    config_mod.save_config(raw)
    log.info("Model browser installed %s (%s) into the user config.", key, entry.get("repo_id"))


def is_browser_entry(key: str) -> bool:
    """Whether ``key`` is a local model the user layer defines (i.e. removable by
    the browser). Recommended models not yet installed are not user-layer entries."""
    cm = config_mod._load_user_config().get("chat_models")
    return isinstance(cm, dict) and key in cm


def is_local_model(key: str) -> bool:
    """Whether ``key`` is an on-device model in the effective catalog (installed
    or a downloaded recommendation). Local models are always fully deletable and
    re-downloadable from Discover, so "Remove from list" deletes them outright;
    Bedrock models use the recoverable ``chat_models_disabled`` hide-list."""
    try:
        from server.local.registry import local_models

        if key in local_models():
            return True
    except Exception:  # pragma: no cover - defensive
        pass
    meta = config_mod.load_config().get("chat_model_meta") or {}
    return isinstance(meta.get(key), dict) and bool(meta[key].get("is_local"))


def build_recommended_entry(key: str) -> tuple[str, dict]:
    """(key, chat_models entry) for a curated Discover recommendation. Keyed by
    its canonical registry key (``local_gemma`` etc.) so the index/summarization
    defaults that reference that key keep working once it is installed. Raises
    KeyError for an unknown recommendation key."""
    from server.local.registry import RECOMMENDED_LOCAL_MODELS

    rec = RECOMMENDED_LOCAL_MODELS[key]
    entry = {
        "id": rec["id"],
        "label": rec.get("label") or key,
        "is_local": True,
        "supports_thinking": bool(rec.get("supports_thinking", False)),
        "supports_tools": bool(rec.get("supports_tools", True)),
        "repo_id": rec["repo_id"],
        "filename": rec["filename"],
        "dir": rec["dir"],
        "ctx": int(rec.get("ctx") or 32768),
    }
    return key, entry


def adopt_local_index_llm() -> bool:
    """After the user installs their first local chat model, point the index LLM
    at the active on-device model — but only if they are still on the fresh-install
    cloud default (``model_mode`` hybrid with ``backends.index_llm`` == "haiku").

    Uses the ``"local"`` alias (follow the available on-device model) rather than a
    fixed key, so it stays valid as models are added or removed. Never overrides an
    explicit user choice (a specific key, "none", or a non-hybrid mode). Returns
    whether it changed anything."""
    raw = config_mod._load_user_config()
    if (raw.get("model_mode") or "") != "hybrid":
        return False
    backends = raw.get("backends")
    if not isinstance(backends, dict) or backends.get("index_llm") != "haiku":
        return False
    backends["index_llm"] = "local"
    raw["backends"] = backends
    config_mod.save_config(raw)
    log.info("First local install: index_llm adopted -> 'local' (follow on-device model).")
    return True


# Marker written into the user layer once the sweep below has run. Without it the
# sweep would re-fire every boot and stomp a user who deliberately turned tools
# back on for a small model — which is a supported choice, just not the default.
_TOOLS_SWEEP_FLAG = "small_model_tools_swept"


def disable_tools_on_small_models() -> list[str]:
    """One-shot: turn ``supports_tools`` off for already-installed local models
    below the agentic threshold, then mark the user layer so it never runs again.

    Installs made before the threshold existed all carry ``supports_tools`` True,
    because that was the hardcoded default — including 1-3B models that answer a
    plain "hey" with a raw JSON tool call. Their entries are the only thing
    keeping them broken, so they are corrected in place ONCE rather than read
    around forever at runtime. Returns the keys it changed.
    """
    raw = config_mod._load_user_config()
    if raw.get(_TOOLS_SWEEP_FLAG):
        return []

    chat_models = raw.get("chat_models")
    changed: list[str] = []
    if isinstance(chat_models, dict):
        for key, entry in chat_models.items():
            if not isinstance(entry, dict) or not entry.get("is_local"):
                continue
            if not entry.get("supports_tools"):
                continue
            # Judge on the same names the install path judged on. An entry with no
            # repo_id (hand-written, weights resolved another way) is left alone.
            if compat.is_agentic_capable(entry.get("repo_id") or "", entry.get("filename") or ""):
                continue
            entry["supports_tools"] = False
            changed.append(key)

    raw[_TOOLS_SWEEP_FLAG] = True
    config_mod.save_config(raw)
    if changed:
        log.info(
            "Turned tool calling off for %d on-device model(s) under %sB: %s. "
            "They are chat-only; re-enable in the config if you want to try.",
            len(changed),
            compat.AGENTIC_MIN_PARAMS_B,
            ", ".join(changed),
        )
    return changed


def remove_entry(key: str) -> bool:
    """Drop ``key`` from the USER layer's ``chat_models``; return whether it was
    present. Leaves the file minimal (drops an emptied ``chat_models``)."""
    raw = config_mod._load_user_config()
    cm = raw.get("chat_models")
    if not isinstance(cm, dict) or key not in cm:
        return False
    cm.pop(key, None)
    if cm:
        raw["chat_models"] = cm
    else:
        raw.pop("chat_models", None)
    config_mod.save_config(raw)
    log.info("Model browser removed %s from the user config.", key)
    return True


def tombstone_entry(key: str) -> bool:
    """Add ``key`` to the USER layer's ``chat_models_disabled`` hide-list so a
    BEDROCK model (no weights, lives in the app-owned catalog) vanishes from the
    list and the composer picker while staying recoverable via Chat model
    visibility. Local models are deleted outright instead (they re-download from
    Discover), so this is Bedrock-only.

    Order-preserving and idempotent — returns whether ``key`` was newly added.
    Writes the same key the visibility toggles use, so both paths stay coherent.
    """
    raw = config_mod._load_user_config()
    disabled_raw = raw.get("chat_models_disabled")
    disabled = (
        [k for k in disabled_raw if isinstance(k, str)] if isinstance(disabled_raw, list) else []
    )
    if key in disabled:
        return False
    disabled.append(key)
    raw["chat_models_disabled"] = disabled
    config_mod.save_config(raw)
    log.info("Model browser tombstoned %s into chat_models_disabled.", key)
    return True
