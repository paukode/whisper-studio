"""Raw USER-config editing over the API (the Settings config editor).

The editor edits the USER layer — config.user.json (or the legacy config.json
while that is still the active user layer). ``GET /api/config/raw`` returns the
user layer text, its path, AND a read-only view of the fully merged effective
config (so the UI can show the shipped models + the user's own side by side).
``PUT`` validates the submitted text — JSON syntax first (with line/column), then
structure (mirroring the local-model rules in server/local/registry.py, a few
known scalars, and the chat_models_disabled hide-list) — reporting EVERY problem
at once, and only then writes through the config module's persist machinery
(atomic write + cache invalidation) so the in-memory config, the local-model
registry, and the models catalog stay coherent.

Validation errors come back as a 400 whose ``detail`` is a newline-separated
list of human messages — the UI renders it as a monospace list, and the shared
API client already surfaces string details as the error message. An unknown name
in chat_models_disabled is a WARNING (logged, not blocking) rather than an error.

Safety rail: the first successful save of a server session snapshots the
previous file to ``<user-config>.bak.<timestamp>`` next to it, using the same
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


def _known_model_keys(data: dict) -> set[str]:
    """Every model key the effective catalog could expose: the SYSTEM catalog,
    the entries in the submitted user config, and the built-in local keys. Used
    to warn (not fail) about chat_models_disabled names that match nothing."""
    keys: set[str] = set()
    try:
        from server.infrastructure.config import _system_config

        system_cm = _system_config().get("chat_models")
        if isinstance(system_cm, dict):
            keys.update(system_cm)
    except Exception:  # pragma: no cover - defensive
        pass
    cm = data.get("chat_models")
    if isinstance(cm, dict):
        keys.update(cm)
    try:
        from server.local.registry import BUILTIN_LOCAL_MODELS

        keys.update(BUILTIN_LOCAL_MODELS)
    except Exception:  # pragma: no cover - defensive
        pass
    return keys


def _validate_structure(data) -> list[str]:
    """Every structural problem in the parsed config, not just the first.
    Unknown keys are fine — the config is extensible by design. An unknown
    chat_models_disabled name is logged as a warning, not returned as an error."""
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

    disabled = data.get("chat_models_disabled")
    if disabled is not None:
        if not isinstance(disabled, list) or not all(isinstance(x, str) for x in disabled):
            errors.append('"chat_models_disabled" must be a list of model-key strings.')
        else:
            known = _known_model_keys(data)
            unknown = [k for k in disabled if k not in known]
            if unknown:
                # Not fatal: the user may be pre-disabling a model that a future
                # app version ships, or just made a typo. Warn, don't block.
                log.warning(
                    "chat_models_disabled names no known model: %s", ", ".join(sorted(unknown))
                )
    return errors


def _backup_once() -> None:
    """Timestamped safety copy of the current user-config file before the
    session's first raw save (same naming as setup.sh: <file>.bak.<YYYYmmddHHMMSS>)."""
    global _backup_done
    if _backup_done:
        return
    path = config_mod._active_user_config_path()
    if os.path.exists(path):
        backup = f"{path}.bak.{time.strftime('%Y%m%d%H%M%S')}"
        try:
            shutil.copyfile(path, backup)
            log.info("Backed up %s to %s before the first raw save.", path, backup)
        except OSError as e:  # best-effort: the user asked to save
            log.warning("Could not back up %s before saving: %s", path, e)
            return  # leave _backup_done False so the next save retries
    _backup_done = True


def _effective_text() -> str:
    """Pretty JSON of the fully merged, read-only effective config (secret
    masked). Best-effort — an empty object if the merge somehow fails, so the
    editor's user pane still loads."""
    from server.workspace import get_workspace_path

    try:
        return json.dumps(config_mod.effective_config_view(get_workspace_path()), indent=2) + "\n"
    except Exception as e:  # pragma: no cover - defensive
        log.warning("Could not build effective config view: %s", e)
        return "{\n}\n"


@router.get("/raw")
async def get_raw_config():
    """The USER layer text and path, plus a read-only view of the fully merged
    effective config so the UI can show the shipped models next to the user's."""
    path = config_mod._active_user_config_path()
    try:
        with open(path) as f:
            user_text = f.read()
    except FileNotFoundError:
        user_text = "{\n}\n"
    return {"user_text": user_text, "path": path, "effective_text": _effective_text()}


@router.put("/raw")
async def put_raw_config(request: Request):
    body = await request.json()
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str):
        raise HTTPException(
            status_code=400, detail='Body must be {"text": "<config.user.json contents>"}.'
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
    # the new contents on their next query. Targets the active user layer.
    config_mod._write_config_text(text if text.endswith("\n") else text + "\n")
    log.info(
        "%s saved via the raw editor (%d bytes).", config_mod._active_user_config_path(), len(text)
    )

    from server.local.registry import local_models

    return {
        "ok": True,
        "path": config_mod._active_user_config_path(),
        # Post-save registry view, so the UI can confirm a new model landed.
        "local_models": sorted(local_models().keys()),
    }
