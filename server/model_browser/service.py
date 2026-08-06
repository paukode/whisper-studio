"""Orchestration for the model browser: search, repo detail, install, uninstall.

Pure-ish glue over :mod:`hf_client` (network) and :mod:`compat` (rules), plus the
existing models-manager download queue and config machinery. No FastAPI here —
routes.py is the thin HTTP shell.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from server.model_browser import compat, hf_client, install

log = logging.getLogger("whisper-studio")

_SORTS = {"trending": "trendingScore", "downloads": "downloads"}
_SEARCH_LIMIT = 30
_PER_AUTHOR_LIMIT = 12


class InstallError(Exception):
    """A well-formed install request that cannot be completed."""


def _sort_key(sort: str) -> str:
    return _SORTS.get(sort, "trendingScore")


def search(
    query: str | None = None,
    author: str | None = None,
    sort: str = "trending",
    all_of_hf: bool = False,
) -> list[dict]:
    """Search GGUF chat repos, keeping only architectures the engine can run.

    Scope (design §8):
      * explicit ``author`` → just that author;
      * default (``all_of_hf`` false) → the trusted-author allowlist, queried in
        parallel and merged;
      * ``all_of_hf`` true → one unscoped search across all of HF.
    Results are de-duped by repo, filtered to SUPPORTED_ARCHS, and ranked by the
    chosen sort metric (descending).
    """
    sort_key = _sort_key(sort)

    if author:
        hits = hf_client.search(query, author, sort_key, _SEARCH_LIMIT)
    elif all_of_hf:
        hits = hf_client.search(query, None, sort_key, _SEARCH_LIMIT)
    else:
        hits = _search_trusted(query, sort_key)

    # De-dupe by repo id, keeping the first (highest-ranked) occurrence.
    seen: set[str] = set()
    out: list[dict] = []
    for h in hits:
        if h.repo_id in seen:
            continue
        seen.add(h.repo_id)
        # Arch gate: the whole point of the browser is to not offer a model that
        # fails at load. An arch we don't recognize as supported is dropped.
        if not compat.arch_supported(h.arch):
            continue
        author_name, _, name = h.repo_id.partition("/")
        out.append(
            {
                "repo_id": h.repo_id,
                "author": author_name,
                "name": name or h.repo_id,
                "label": name or h.repo_id,
                "downloads": h.downloads,
                "likes": h.likes,
                "trending_score": h.trending_score,
                "arch": h.arch,
                "param_size": compat.param_size_label(h.repo_id),
                "context_length": h.context_length,
                "gated": h.gated,
                "supported": True,
            }
        )

    rank = "trending_score" if sort_key == "trendingScore" else "downloads"
    out.sort(key=lambda r: (r.get(rank) or 0), reverse=True)
    return out[:_SEARCH_LIMIT]


def _search_trusted(query: str | None, sort_key: str) -> list[hf_client.SearchHit]:
    """Query each trusted author in parallel and flatten the results."""
    authors = compat.TRUSTED_AUTHORS
    results: list[hf_client.SearchHit] = []
    with ThreadPoolExecutor(max_workers=len(authors)) as pool:
        futures = [
            pool.submit(hf_client.search, query, a, sort_key, _PER_AUTHOR_LIMIT) for a in authors
        ]
        for f in futures:
            try:
                results.extend(f.result())
            except Exception as e:  # pragma: no cover - defensive
                log.debug("Trusted-author search shard failed: %s", e)
    return results


def repo_detail(repo_id: str) -> dict:
    """The quant picker for one repo: supported flag, arch, context length, and
    one row per quant (shards folded, mmproj hidden, recommended pre-selected)."""
    meta = hf_client.repo_gguf_meta(repo_id)
    files = hf_client.repo_files(repo_id)
    options = compat.group_gguf_files(files)
    recommended = compat.recommended_quant(options)
    return {
        "repo_id": repo_id,
        "arch": meta.arch,
        "supported": compat.arch_supported(meta.arch),
        "context_length": meta.context_length,
        "has_chat_template": meta.has_chat_template,
        "gated": hf_client.repo_is_gated(repo_id),
        "recommended_filename": recommended,
        "quants": [
            {
                "quant": o.quant,
                "filename": o.filename,
                "size_bytes": o.size_bytes,
                "is_sharded": o.is_sharded,
                "shard_count": o.shard_count,
                "recommended": o.filename == recommended,
            }
            for o in options
        ],
    }


def install_model(
    repo_id: str,
    filename: str,
    label: str | None = None,
    n_ctx: int | None = None,
) -> dict:
    """Write the config entry and enqueue the download. Returns the new key +
    the download start result. Raises InstallError on an unsupported/invalid
    request so the route can answer 400/409 with a clear reason."""
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
    each row carrying its canonical install key, one-click filename/quant, and a
    ``downloaded`` flag so the UI can badge already-present models."""
    from server.local import registry

    rows: list[dict] = []
    for key, meta in registry.recommended_models().items():
        repo = meta["repo_id"]
        author, _, name = repo.partition("/")
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
