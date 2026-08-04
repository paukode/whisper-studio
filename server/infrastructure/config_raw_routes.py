"""Raw config.json editing over the API (the Settings "config.json" editor).

``GET /api/config/raw`` returns the file text verbatim; ``PUT`` validates it —
JSON syntax first (with line/column), then structure (mirroring the local-model
rules in server/local/registry.py plus a few known scalars) — reporting EVERY
problem at once, and only then writes through the config module's persist
machinery (atomic write + cache invalidation) so the in-memory config, the
local-model registry, and the models catalog stay coherent.

Validation errors come back as a 400 whose ``detail`` is a newline-separated
list of human messages — the UI renders it as a monospace list, and the shared
API client already surfaces string details as the error message.

Safety rail: the first successful save of a server session snapshots the
previous file to ``config.json.bak.<timestamp>`` next to it, using the same
naming convention as setup.sh's pre-overwrite backups.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time

from fastapi import APIRouter, HTTPException, Request

from server.infrastructure import config as config_mod

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/config", tags=["config"])

_VALID_MODEL_MODES = ("cloud", "hybrid", "local")
# The weight fields an is_local chat_models entry must carry to be loadable
# (server/local/registry.py); built-in keys may omit them — the built-in fills.
_LOCAL_REQUIRED = ("repo_id", "filename", "dir")

# One backup per server session: the first raw save snapshots the file the
# session started from; later saves in the same session overwrite freely
# (the user is iterating in the editor, not destroying an unrelated state).
_backup_done = False


def _nonempty_str(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _validate_chat_model_entry(key: str, val, errors: list[str]) -> None:
    """Structural rules for ONE chat_models entry, mirroring what
    _normalize_chat_models and server/local/registry.py actually require."""
    where = f"chat_models.{key}"
    if isinstance(val, str):
        if not val.strip():
            errors.append(f"{where}: a model id string must not be empty.")
        return
    if not isinstance(val, dict):
        errors.append(f'{where}: must be a model id string or an object with an "id" field.')
        return

    for field in ("id", "label"):
        if field in val and not _nonempty_str(val[field]):
            errors.append(f'{where}: "{field}" must be a non-empty string.')

    if not val.get("is_local"):
        return

    # Local (on-device) model rules — server/local/registry.py. An entry
    # without a valid "id" is dropped by the normalizer before the registry
    # ever sees it, so id is effectively required here.
    if "id" not in val:
        errors.append(
            f'{where}: a local model entry needs an "id" string starting with "local:" '
            f'(e.g. "local:my-model").'
        )
    elif _nonempty_str(val["id"]) and not val["id"].startswith("local:"):
        errors.append(f'{where}: a local model "id" must start with "local:" (got {val["id"]!r}).')

    try:  # built-ins may fill missing weight fields for the same key
        from server.local.registry import BUILTIN_LOCAL_MODELS

        builtin = BUILTIN_LOCAL_MODELS.get(key, {})
    except Exception:  # pragma: no cover - defensive
        builtin = {}
    for field in _LOCAL_REQUIRED:
        if not _nonempty_str(val.get(field, builtin.get(field))):
            errors.append(
                f'{where}: local model is missing a non-empty string "{field}" '
                f"(is_local entries need repo_id, filename and dir to be loadable)."
            )

    ctx = val.get("ctx")
    if ctx is not None and (isinstance(ctx, bool) or not isinstance(ctx, int) or ctx <= 0):
        errors.append(f'{where}: "ctx" must be a positive integer (context window in tokens).')


def _validate_structure(data) -> list[str]:
    """Every structural problem in the parsed config, not just the first.
    Unknown keys are fine — the config is extensible by design."""
    if not isinstance(data, dict):
        return ["The top level must be a JSON object: { ... }."]

    errors: list[str] = []
    if "bedrock_region" in data and not isinstance(data["bedrock_region"], str):
        errors.append('"bedrock_region" must be a string (e.g. "us-east-1").')
    if "model_mode" in data and data["model_mode"] not in _VALID_MODEL_MODES:
        errors.append('"model_mode" must be one of "cloud", "hybrid" or "local".')
    if "tavily_api_key" in data and not isinstance(data["tavily_api_key"], str):
        errors.append('"tavily_api_key" must be a string.')

    chat_models = data.get("chat_models")
    if chat_models is not None and not isinstance(chat_models, dict):
        errors.append('"chat_models" must be an object mapping model keys to entries.')
    elif isinstance(chat_models, dict):
        for key, val in chat_models.items():
            _validate_chat_model_entry(key, val, errors)
    return errors


def _backup_once() -> None:
    """Timestamped safety copy of the current file before the session's first
    raw save (same naming as setup.sh: config.json.bak.<YYYYmmddHHMMSS>)."""
    global _backup_done
    if _backup_done:
        return
    path = config_mod.CONFIG_PATH
    if os.path.exists(path):
        backup = f"{path}.bak.{time.strftime('%Y%m%d%H%M%S')}"
        try:
            shutil.copyfile(path, backup)
            log.info("Backed up config.json to %s before the first raw save.", backup)
        except OSError as e:  # best-effort: the user asked to save
            log.warning("Could not back up config.json before saving: %s", e)
            return  # leave _backup_done False so the next save retries
    _backup_done = True


@router.get("/raw")
async def get_raw_config():
    """The literal on-disk config.json text (an empty object when missing)."""
    path = config_mod.CONFIG_PATH
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        text = "{\n}\n"
    return {"text": text, "path": path}


@router.put("/raw")
async def put_raw_config(request: Request):
    body = await request.json()
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str):
        raise HTTPException(
            status_code=400, detail='Body must be {"text": "<config.json contents>"}.'
        )

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Not valid JSON at line {e.lineno}, column {e.colno}: {e.msg}.",
        ) from e

    problems = _validate_structure(data)
    if problems:
        raise HTTPException(status_code=400, detail="\n".join(problems))

    _backup_once()
    # Through the config module's own persist path: atomic write + cache
    # invalidation, so the local-model registry and models catalog re-read
    # the new contents on their next query.
    config_mod._write_config_text(text if text.endswith("\n") else text + "\n")
    log.info("config.json saved via the raw editor (%d bytes).", len(text))

    from server.local.registry import local_models

    return {
        "ok": True,
        "path": config_mod.CONFIG_PATH,
        # Post-save registry view, so the UI can confirm a new model landed.
        "local_models": sorted(local_models().keys()),
    }
