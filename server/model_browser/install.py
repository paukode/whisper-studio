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
      * ``supports_tools`` True — the local tool loop rides on llama-server
        ``--jinja``, which these instruct families support;
      * ``supports_thinking`` True only for reasoning families (qwen3, QwQ,
        DeepSeek-R1), else False.
    """
    key = registry_key(repo_id, quant)
    entry = {
        "id": _model_id(repo_id, quant),
        "label": label or f"{repo_id.split('/')[-1]} {quant} (Local)",
        "is_local": True,
        "supports_thinking": compat.is_thinking_model(arch, repo_id, filename),
        "supports_tools": True,
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
    the browser). Built-ins and shipped models are not user-layer entries."""
    cm = config_mod._load_user_config().get("chat_models")
    return isinstance(cm, dict) and key in cm


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
    SHIPPED model (one we cannot delete from the config) vanishes from the list
    and the composer picker while staying recoverable via Chat model visibility.

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
