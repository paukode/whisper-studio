"""Orchestration for the model browser: search, repo detail, install, uninstall.

Pure-ish glue over :mod:`hf_client` (network) and :mod:`compat` (rules), plus the
existing models-manager download queue and config machinery. No FastAPI here —
routes.py is the thin HTTP shell.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from server.model_browser import compat, hf_client, install

log = logging.getLogger("whisper-studio")

_SEARCH_LIMIT = 30
_PER_AUTHOR_LIMIT = 12


class InstallError(Exception):
    """A well-formed install request that cannot be completed."""


@lru_cache(maxsize=1)
def _total_memory_bytes() -> int | None:
    """Physical RAM of the machine THIS process is running on, measured at
    call time — never a constant for any particular machine. Works on macOS
    and Linux via ``os.sysconf``; falls back to ``sysctl`` for a Python build
    that doesn't expose those keys. ``None`` if neither path succeeds, so
    callers omit the fit verdict rather than guess."""
    try:
        return int(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    except (ValueError, OSError, AttributeError):
        pass
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5
        )
        return int(out.stdout.strip())
    except Exception as e:  # pragma: no cover - environment dependent
        log.debug("Could not determine total memory: %s", e)
        return None


def _dir_size_bytes(path: str) -> int:
    """Recursive on-disk byte count of ``path``, or 0 if it doesn't exist."""
    if not path or not os.path.isdir(path):
        return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                continue
    return total


@lru_cache(maxsize=1)
def _reserved_companion_bytes() -> int:
    """On-disk size of the companion models that plausibly stay resident in
    memory ALONGSIDE a chat session on THIS machine: whichever single ASR
    backend is actually configured (dictation can run while chatting), plus
    the small speaker-diarization model used during the same transcription.

    The workspace-index models (embed/GLiNER/GLiNER2/rerank) are deliberately
    NOT counted: they lazy-load only for the duration of one indexing pass and
    are explicitly unloaded the moment it finishes (see pipeline.build's
    ``finally`` block), so they are not steady-state competitors for a chat
    model's memory. Reserving their full on-disk size (which can exceed 10 GB
    across all four) as if permanently resident was the original bug — it
    could shrink the usable budget so far that even a 2-3 GB quant, or an
    already-installed and working local model, was graded "won't fit".

    Only ONE ASR backend is ever loaded (``transcription_backend`` picks one),
    so only that one's directory is measured — never both. Imports lazily so
    this stays cheap to call from the read-only /browse endpoints."""
    dirs: list[str] = []
    try:
        from server.asr import resolve_name
        from server.infrastructure.config import load_config

        backend = resolve_name(load_config().get("transcription_backend"))
        if backend == "parakeet":
            from server.asr.parakeet_backend import PARAKEET_MODEL_DIR

            dirs.append(PARAKEET_MODEL_DIR)
        else:
            from server.asr.whisper_backend import WHISPER_MODEL_DIR

            dirs.append(WHISPER_MODEL_DIR)
    except Exception as e:  # pragma: no cover - defensive
        log.debug("Could not resolve the active ASR model dir: %s", e)
    try:
        from server.diarization.speakers import SPEAKER_MODEL_DIR

        dirs.append(SPEAKER_MODEL_DIR)
    except Exception as e:  # pragma: no cover - defensive
        log.debug("Could not resolve speaker model dir: %s", e)

    return sum(_dir_size_bytes(d) for d in dirs)


def _memory_fields() -> dict:
    """The three memory numbers the Discover UI needs to word a fit warning in
    the user's own terms, all measured on the running machine: total RAM, the
    slice of it a chat model can claim, and how much of that slice is already
    spoken for by other resident models."""
    total = _total_memory_bytes()
    reserved = _reserved_companion_bytes()
    budget = None
    if total is not None:
        budget = max(0, compat.MEM_BUDGET_FRACTION * total - reserved)
    return {
        "mem_total_bytes": total,
        "mem_reserved_bytes": reserved,
        "mem_budget_bytes": int(budget) if budget is not None else None,
    }


def _norm_fmt(fmt: str | None) -> str:
    return "mlx" if fmt == "mlx" else "gguf"


def _squash(text: str) -> str:
    """Lowercase with every non-alphanumeric run removed, so "qwen 3.8",
    "qwen-3.8" and "Qwen3.8" all compare equal."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _relevance_tier(query: str, name: str, repo_id: str) -> int:
    """How directly a repo matches the typed query, spelling variants folded:
    3 = the repo name IS the query (or starts with it), 2 = the query appears
    inside the name, 1 = every query token appears somewhere in the full repo
    id, 0 = only a loose match (the Hub matched a field we don't score)."""
    q = _squash(query)
    if not q:
        return 0
    name_sq = _squash(name)
    if name_sq == q or name_sq.startswith(q):
        return 3
    if q in name_sq:
        return 2
    tokens = re.findall(r"[a-z0-9]+", query.lower())
    repo_sq = _squash(repo_id)
    if tokens and all(t in repo_sq for t in tokens):
        return 1
    return 0


def _install_annotations(fmt: str) -> dict[str, dict]:
    """``repo_id -> {"installed": [filenames], "downloading": [filenames]}`` for
    every registry model of this format, so search rows can say "you already
    have this" instead of letting the user queue the same multi-GB download
    twice. Download state comes from the models-manager queue (the SAME source
    the Chat tab renders). Filenames let the GGUF quant picker mark the exact
    quant; MLX entries have none (the repo is the unit). Never raises — an
    annotation failure must not sink the search."""
    try:
        from server.local.registry import local_models
        from server.models_manager import catalog, manager

        by_key = {e.key: e for e in catalog.entries() if e.group == catalog.GROUP_LOCAL_CHAT}
        out: dict[str, dict] = {}
        for key, m in local_models().items():
            if (m.get("engine") == "mlx") != (fmt == "mlx"):
                continue
            entry = by_key.get(key)
            if entry is None:
                continue
            state, _err = manager.state_of(entry)
            slot = out.setdefault(m["repo_id"], {"installed": [], "downloading": []})
            if state == "installed":
                slot["installed"].append(m.get("filename") or "")
            elif state in ("downloading", "queued"):
                slot["downloading"].append(m.get("filename") or "")
        return out
    except Exception as e:  # pragma: no cover - defensive
        log.debug("Could not annotate install states: %s", e)
        return {}


def _downloading_keys() -> set[str]:
    """Registry keys whose download is running or queued right now. Lets the
    Recommended section disable its button mid-download instead of only after
    the weights land. Never raises."""
    try:
        from server.models_manager import catalog, manager

        keys: set[str] = set()
        for e in catalog.entries():
            if e.group != catalog.GROUP_LOCAL_CHAT:
                continue
            state, _err = manager.state_of(e)
            if state in ("downloading", "queued"):
                keys.add(e.key)
        return keys
    except Exception as e:  # pragma: no cover - defensive
        log.debug("Could not read downloading keys: %s", e)
        return set()


def search(
    query: str | None = None,
    author: str | None = None,
    all_of_hf: bool = False,
) -> list[dict]:
    """Search chat repos across BOTH weight formats at once, keeping only
    architectures the matching engine can run (GGUF → llama-server,
    MLX → mlx_lm).

    Scope (design §8):
      * explicit ``author`` → just that author, both formats;
      * default (``all_of_hf`` false) → each format's trusted-author allowlist;
      * ``all_of_hf`` true → one unscoped search per format across all of HF.
    All shards are queried in parallel and merged. Results are de-duped by
    repo (GGUF lane first — its per-quant picker is the more flexible
    install), filtered to each format's supported archs, and ranked by
    relevance to the typed query with downloads as the tie-break; an empty
    query ranks by trending instead.
    """
    if author:
        shards = [("gguf", author), ("mlx", author)]
    elif all_of_hf:
        shards = [("gguf", None), ("mlx", None)]
    else:
        shards = [("gguf", a) for a in compat.TRUSTED_AUTHORS] + [
            ("mlx", a) for a in compat.MLX_TRUSTED_AUTHORS
        ]
    limit = _SEARCH_LIMIT if len(shards) <= 2 else _PER_AUTHOR_LIMIT

    hits: list[tuple[str, hf_client.SearchHit]] = []
    with ThreadPoolExecutor(max_workers=len(shards)) as pool:
        futures = {pool.submit(hf_client.search, query, a, limit, f): f for f, a in shards}
        for future, f in futures.items():
            try:
                hits.extend((f, h) for h in future.result())
            except Exception as e:  # pragma: no cover - defensive
                log.debug("Search shard failed (fmt=%s): %s", f, e)

    # GGUF hits ahead of MLX so a repo publishing both formats de-dupes into
    # the lane with the per-quant picker (stable sort keeps shard order).
    hits.sort(key=lambda pair: pair[0] != "gguf")

    # De-dupe by repo id, keeping the first occurrence.
    seen: set[str] = set()
    out: list[dict] = []
    for fmt, h in hits:
        if h.repo_id in seen:
            continue
        seen.add(h.repo_id)
        # Arch gate: the whole point of the browser is to not offer a model that
        # fails at load. An arch we don't recognize as supported is dropped.
        arch_ok = compat.mlx_arch_supported if fmt == "mlx" else compat.arch_supported
        if not arch_ok(h.arch):
            continue
        author_name, _, name = h.repo_id.partition("/")
        out.append(
            {
                "repo_id": h.repo_id,
                "author": author_name,
                "name": name or h.repo_id,
                "label": name or h.repo_id,
                "format": fmt,
                "downloads": h.downloads,
                "likes": h.likes,
                "trending_score": h.trending_score,
                "arch": h.arch,
                "param_size": compat.param_size_label(h.repo_id),
                # Whether this model would be installed with tools ON, so the row
                # can say "chat only" BEFORE a multi-GB download rather than
                # leaving the user to discover it from a broken first turn.
                "tool_capable": compat.is_agentic_capable(h.repo_id),
                "context_length": h.context_length,
                "gated": h.gated,
                "supported": True,
            }
        )

    annotations = {f: _install_annotations(f) for f in ("gguf", "mlx")}
    for row in out:
        ann = annotations[row["format"]].get(row["repo_id"], {})
        installed = ann.get("installed") or []
        downloading = ann.get("downloading") or []
        row["installed_filenames"] = installed
        row["downloading_filenames"] = downloading
        row["install_state"] = "installed" if installed else "downloading" if downloading else None

    if query and query.strip():
        out.sort(
            key=lambda r: (
                -_relevance_tier(query, r["name"], r["repo_id"]),
                -(r.get("downloads") or 0),
            )
        )
    else:
        out.sort(key=lambda r: (r.get("trending_score") or 0), reverse=True)
    return out[:_SEARCH_LIMIT]


def repo_detail(repo_id: str, fmt: str = "gguf") -> dict:
    """The quant picker for one repo: supported flag, arch, context length, and
    one row per quant (shards folded, mmproj hidden, recommended pre-selected,
    each carrying a fit verdict for the machine this server runs on).

    An MLX repo IS one quant (the tag is in the repo name), so its detail
    carries a single pseudo-option whose size is the whole snapshot.
    """
    if _norm_fmt(fmt) == "mlx":
        return _mlx_repo_detail(repo_id)
    meta = hf_client.repo_gguf_meta(repo_id)
    files = hf_client.repo_files(repo_id)
    options = compat.group_gguf_files(files)
    recommended = compat.recommended_quant(options)
    mem = _memory_fields()
    total_mem = mem["mem_total_bytes"]
    reserved = mem["mem_reserved_bytes"]
    return {
        "repo_id": repo_id,
        "format": "gguf",
        "arch": meta.arch,
        "supported": compat.arch_supported(meta.arch),
        "context_length": meta.context_length,
        "has_chat_template": meta.has_chat_template,
        "gated": hf_client.repo_is_gated(repo_id),
        "recommended_filename": recommended,
        "param_size": compat.param_size_label(repo_id),
        "tool_capable": compat.is_agentic_capable(repo_id),
        "agentic_min_params_b": compat.AGENTIC_MIN_PARAMS_B,
        **mem,
        "quants": [
            {
                "quant": o.quant,
                "filename": o.filename,
                "size_bytes": o.size_bytes,
                "is_sharded": o.is_sharded,
                "shard_count": o.shard_count,
                "recommended": o.filename == recommended,
                "fit": compat.fit_verdict(o.size_bytes, total_mem, reserved),
                "needed_bytes": compat.needed_bytes(o.size_bytes),
            }
            for o in options
        ],
    }


def _mlx_repo_detail(repo_id: str) -> dict:
    model_type, mlx_tagged = hf_client.repo_mlx_meta(repo_id)
    files = hf_client.repo_files(repo_id)
    total_size = sum(size for _name, size in files)
    quant = compat.parse_mlx_quant(repo_id) or "MLX"
    mem = _memory_fields()
    fit = compat.fit_verdict(total_size or None, mem["mem_total_bytes"], mem["mem_reserved_bytes"])
    return {
        "repo_id": repo_id,
        "format": "mlx",
        "arch": model_type,
        "supported": compat.mlx_arch_supported(model_type) and mlx_tagged,
        "context_length": None,
        "has_chat_template": True,  # converted MLX chat repos carry the template
        "gated": hf_client.repo_is_gated(repo_id),
        # The whole snapshot is the one installable option; filename "" tells
        # the UI there is no per-file choice to make.
        "recommended_filename": "",
        "param_size": compat.param_size_label(repo_id),
        "tool_capable": compat.is_agentic_capable(repo_id),
        "agentic_min_params_b": compat.AGENTIC_MIN_PARAMS_B,
        **mem,
        "quants": [
            {
                "quant": quant,
                "filename": "",
                "size_bytes": total_size,
                "is_sharded": False,
                "shard_count": 1,
                "recommended": True,
                "fit": fit,
                "needed_bytes": compat.needed_bytes(total_size or None),
            }
        ],
    }


def install_model(
    repo_id: str,
    filename: str,
    label: str | None = None,
    n_ctx: int | None = None,
    fmt: str = "gguf",
) -> dict:
    """Write the config entry and enqueue the download. Returns the new key +
    the download start result. Raises InstallError on an unsupported/invalid
    request so the route can answer 400/409 with a clear reason."""
    if _norm_fmt(fmt) == "mlx":
        return _install_mlx_model(repo_id, label=label, n_ctx=n_ctx)
    if not repo_id or not filename:
        raise InstallError("repo_id and filename are required.")
    if not filename.lower().endswith(".gguf"):
        raise InstallError("filename must be a .gguf file.")

    # Re-verify the arch server-side (never trust the client): read the header
    # and refuse an unsupported architecture before writing anything.
    meta = hf_client.repo_gguf_meta(repo_id)
    if not compat.arch_supported(meta.arch):
        raise InstallError(
            f"{repo_id} reports architecture {meta.arch!r}, which the bundled model "
            "engine cannot run. It was not installed."
        )

    quant = compat.parse_quant(filename) or "GGUF"
    key, entry = install.build_entry(
        repo_id=repo_id,
        filename=filename,
        quant=quant,
        arch=meta.arch,
        label=label,
        n_ctx=n_ctx,
        header_ctx=meta.context_length,
    )
    return _finalize_install(key, entry)


def _install_mlx_model(repo_id: str, label: str | None = None, n_ctx: int | None = None) -> dict:
    """The MLX fork of install_model: whole-repo snapshot, arch re-verified from
    the transformers config instead of a GGUF header."""
    if not repo_id:
        raise InstallError("repo_id is required.")

    model_type, mlx_tagged = hf_client.repo_mlx_meta(repo_id)
    if not mlx_tagged:
        # A GGUF repo usually carries a config.json too, so the arch gate alone
        # would happily accept it — and snapshot gigabytes mlx_lm cannot load.
        raise InstallError(
            f"{repo_id} does not carry MLX-format weights (no mlx tag on the "
            "repo). It was not installed."
        )
    if not compat.mlx_arch_supported(model_type):
        raise InstallError(
            f"{repo_id} reports model_type {model_type!r}, which the bundled "
            "mlx-lm engine cannot run. It was not installed."
        )

    key, entry = install.build_mlx_entry(
        repo_id=repo_id,
        arch=model_type,
        label=label,
        n_ctx=n_ctx,
        # The real trained window from the repo's config.json, so the context
        # meter and prompt budgeting see the model's true size (capped by
        # resolve_ctx the same way a GGUF header ctx is).
        header_ctx=hf_client.repo_config_context_length(repo_id),
    )
    return _finalize_install(key, entry)


def install_recommended(key: str) -> dict:
    """Install a curated Discover recommendation by its canonical key. Writes the
    shipped definition into the user config and enqueues the download — same tail
    as a searched install, but the metadata comes from the recommendation, not a
    live HF header read."""
    from server.local.registry import RECOMMENDED_LOCAL_MODELS

    if key not in RECOMMENDED_LOCAL_MODELS:
        raise InstallError(f"{key!r} is not a recommended model.")
    reg_key, entry = install.build_recommended_entry(key)
    return _finalize_install(reg_key, entry)


def _finalize_install(key: str, entry: dict) -> dict:
    """Write the config entry, enqueue the download, and (on the user's first
    local install) adopt the on-device model for the index LLM. Shared by the
    searched-install and recommended-install paths."""
    install.write_entry(key, entry)

    # The registry now merges the entry in, so the catalog derives a download
    # row for it. Enqueue through the existing queue for progress/cancel/retry.
    from server.models_manager import manager
    from server.models_manager.catalog import get_entry

    catalog_entry = get_entry(key)
    if catalog_entry is None:  # pragma: no cover - would mean the registry didn't pick it up
        install.remove_entry(key)
        raise InstallError(
            "The model was written to config but did not resolve in the registry; "
            "check the config editor for a validation problem."
        )
    try:
        start = manager.start_download(catalog_entry)
    except manager.Conflict as e:
        # Already installed on disk — the config entry is still what we want.
        start = "already_installed"
        log.info("Install %s: %s", key, e)

    # Fresh installs default the index LLM to cloud (no local model shipped);
    # installing the first local chat model flips it to follow the on-device model.
    adopted_index_llm = install.adopt_local_index_llm()

    return {
        "key": key,
        "id": entry["id"],
        "label": entry["label"],
        "ctx": entry["ctx"],
        "supports_thinking": entry["supports_thinking"],
        "supports_tools": entry["supports_tools"],
        "download": start,
        "adopted_index_llm": adopted_index_llm,
    }


def recommended() -> list[dict]:
    """The curated on-device catalog for the Discover tab's Recommended section,
    each row carrying its canonical install key, one-click filename/quant, a
    ``downloaded`` flag so the UI can badge already-present models, and a fit
    verdict for the machine this server runs on."""
    from server.local import registry

    mem = _memory_fields()
    total_mem = mem["mem_total_bytes"]
    reserved = mem["mem_reserved_bytes"]
    downloading_keys = _downloading_keys()

    rows: list[dict] = []
    for key, meta in registry.recommended_models().items():
        repo = meta["repo_id"]
        author, _, name = repo.partition("/")
        size_bytes = meta.get("size_bytes")
        rows.append(
            {
                "key": key,
                "repo_id": repo,
                "author": author,
                "name": name or repo,
                "label": meta.get("label") or key,
                "filename": meta["filename"],
                "quant": compat.parse_quant(meta["filename"]) or "GGUF",
                "param_size": compat.param_size_label(repo),
                "ctx": meta.get("ctx"),
                "supports_thinking": bool(meta.get("supports_thinking")),
                "supports_tools": bool(meta.get("supports_tools")),
                "downloaded": bool(meta.get("downloaded")),
                "downloading": key in downloading_keys,
                "size_bytes": size_bytes,
                "fit": compat.fit_verdict(size_bytes, total_mem, reserved),
                "needed_bytes": compat.needed_bytes(size_bytes),
            }
        )
    return rows


def _delete_weights(key: str) -> tuple[bool, bool]:
    """Cancel any running/queued download and delete this model's weights.

    Returns ``(stopped_llama_server, weights_deleted)``. Missing files are not
    an error — a model with a config entry but nothing on disk just deletes
    nothing. Shared by uninstall (user entry) and remove-from-list (either)."""
    from server.models_manager import manager
    from server.models_manager.catalog import get_entry

    entry = get_entry(key)
    if entry is None:
        return False, False
    # Cancel a running/queued download first so delete isn't refused.
    try:
        manager.cancel(entry)
    except manager.Conflict:
        pass
    try:
        result = manager.delete(entry)
        return bool(result.get("stopped_llama_server")), bool(result.get("deleted"))
    except manager.Conflict:
        # Nothing on disk to delete — fine.
        return False, False


def uninstall_model(key: str) -> dict:
    """Stop/cancel any activity, delete files, and remove the user-config entry.

    Only browser-installed (user-layer) keys are removable here; a built-in or
    shipped model is not touched."""
    if not install.is_browser_entry(key):
        raise InstallError(f"{key} is not a browser-installed model.")
    stopped, _ = _delete_weights(key)
    removed = install.remove_entry(key)
    return {"key": key, "removed": removed, "stopped_llama_server": stopped}


def remove_from_list(key: str) -> dict:
    """Remove a chat model from the Models list and the composer picker.

    A LOCAL model (installed or a downloaded recommendation) is DELETED outright
    — its weights and its config entry both go — because it is always
    re-downloadable from the Discover tab, so there is nothing to preserve.
    A BEDROCK model has no weights and lives in the app-owned catalog, so it is
    hidden via the recoverable ``chat_models_disabled`` list instead.
    Any running/queued download is cancelled first so the delete isn't refused.
    """
    stopped, weights_deleted = _delete_weights(key)

    if install.is_local_model(key):
        removed_entry = install.remove_entry(key)
        return {
            "key": key,
            "scope": "local",
            "removed_entry": removed_entry,
            "weights_deleted": weights_deleted,
            "stopped_llama_server": stopped,
        }

    tombstoned = install.tombstone_entry(key)
    return {
        "key": key,
        "scope": "bedrock",
        "tombstoned": tombstoned,
        "weights_deleted": weights_deleted,
        "stopped_llama_server": stopped,
    }
