"""Account-level Bedrock data-retention control.

Mythos-class models (e.g. Claude Fable 5) refuse to run unless the AWS account's
Bedrock data-retention mode is ``provider_data_share``. The default/inherit mode is
rejected at invoke time with::

    ValidationException: data retention mode 'default' is not available for this model

This module wraps the Bedrock CONTROL-PLANE API (``boto3.client("bedrock")`` — not
``bedrock-runtime``) so the UI can flip the account into ``provider_data_share``
just-in-time when the user opts into such a model, and back to explicit zero
retention (``none``) when they switch away.

⚠️ The setting is ACCOUNT-WIDE for the configured region — it applies to ALL Bedrock
traffic on the account, not only the model that triggered it. The consent screen in
the UI exists to make that explicit before flipping it on.
"""

import logging
import threading
import time

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server.infrastructure.config import load_config

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/data-retention", tags=["data-retention"])

# Valid modes per the Bedrock PutAccountDataRetention API:
#   default | none | provider_data_share | inherit
SHARING_MODE = "provider_data_share"
# Policy: when sharing is turned off, the account goes to explicit zero
# retention ("none") — nothing is retained or shared for any model unless a
# Mythos-class model is actively in use. Deliberately NOT a save/restore of
# the previous mode: the desired resting state is always "none".
DEFAULT_RESTORE_MODE = "none"

# Cached control-plane client per region (boto3 clients are thread-safe and own a
# connection pool — same fd-conservation rationale as server/chat/infra.py).
_clients: dict[str, object] = {}
_clients_lock = threading.Lock()


def _get_control_plane_client():
    region = load_config().get("bedrock_region", "us-east-1")
    with _clients_lock:
        client = _clients.get(region)
        if client is None:
            client = boto3.client(
                "bedrock",
                region_name=region,
                config=BotoConfig(
                    connect_timeout=10,
                    read_timeout=30,
                    retries={"max_attempts": 2},
                ),
            )
            _clients[region] = client
        return client


def get_mode() -> str:
    """Return the account's current Bedrock data-retention mode."""
    return _get_control_plane_client().get_account_data_retention().get("mode", "")


# Short-lived cache of the account mode so a burst of agent spawns doesn't make
# one control-plane call each. The mode changes rarely (only when the user
# toggles retention), so a small TTL is safe. Failures are negative-cached with
# a shorter TTL: a fan-out of agents on a broken control plane (missing IAM
# permission, endpoint down) must not pay a fresh blocking round trip each.
_MODE_TTL_SECONDS = 60.0
_MODE_FAILURE_TTL_SECONDS = 30.0
_MODE_UNKNOWN = "__unknown__"
_mode_cache: dict[str, object] = {"mode": None, "ts": 0.0}
_mode_lock = threading.Lock()


def get_mode_cached() -> str | None:
    """Return the account retention mode, cached for a short TTL. Returns None
    when it cannot be determined (missing IAM permission, transient error), so
    callers can fail OPEN rather than block on an unknown."""
    now = time.monotonic()
    with _mode_lock:
        cached = _mode_cache.get("mode")
        ts = float(_mode_cache.get("ts", 0.0) or 0.0)
        if cached == _MODE_UNKNOWN and (now - ts) < _MODE_FAILURE_TTL_SECONDS:
            return None
        if cached is not None and cached != _MODE_UNKNOWN and (now - ts) < _MODE_TTL_SECONDS:
            return str(cached)
    try:
        mode = get_mode()
    except Exception as e:  # noqa: BLE001 — unknown mode => fail open
        log.debug("data-retention mode lookup failed (failing open): %s", e)
        with _mode_lock:
            _mode_cache["mode"] = _MODE_UNKNOWN
            _mode_cache["ts"] = now
        return None
    with _mode_lock:
        _mode_cache["mode"] = mode
        _mode_cache["ts"] = now
    return mode


def model_requires_data_retention(model_id: str) -> bool:
    """True if the chat_models entry for this Bedrock id is flagged
    ``requires_data_retention`` (Mythos-class models like Fable 5).

    load_config() NORMALIZES chat_models to a flat ``{key: id_string}`` map and
    parks the rich per-model dicts under ``chat_model_meta`` — so the flag must
    be read by joining the two. (The original implementation iterated
    chat_models looking for dicts and therefore never matched anything in
    production: the gate was dead code and Fable agents still hit the raw
    ValidationException.) The dict branch is kept for callers/tests that pass
    an un-normalized rich map.
    """
    if not model_id:
        return False
    try:
        cfg = load_config()
        models = cfg.get("chat_models", {}) or {}
        meta = cfg.get("chat_model_meta", {}) or {}
    except Exception:
        return False
    for key, m in models.items():
        # Normalized shape: value is the Bedrock id string; flag lives in meta.
        if isinstance(m, str) and m == model_id:
            mm = meta.get(key)
            return bool(isinstance(mm, dict) and mm.get("requires_data_retention"))
        # Un-normalized rich shape (defensive).
        if isinstance(m, dict) and m.get("id") == model_id:
            return bool(m.get("requires_data_retention"))
    return False


def retention_block_reason(model_id: str) -> str | None:
    """Return a user-facing reason string if ``model_id`` cannot run under the
    account's current retention mode, else None.

    Fails OPEN (returns None) when the model doesn't require retention or when
    the account mode can't be read — so a transient control-plane error never
    blocks an agent that would otherwise succeed. Never flips the account mode
    itself: enabling account-wide data sharing is a consented action the user
    takes in the UI, not something an agent does silently."""
    if not model_requires_data_retention(model_id):
        return None
    mode = get_mode_cached()
    if mode is None or mode == SHARING_MODE:
        return None
    return (
        f"This model requires Bedrock data retention (account mode "
        f"'{SHARING_MODE}'), but the account is currently '{mode}'. Re-select "
        "the model in the UI to enable retention, or set the account "
        "data-retention mode to 'provider_data_share'."
    )


def _reset_mode_cache() -> None:
    """Test hook."""
    with _mode_lock:
        _mode_cache["mode"] = None
        _mode_cache["ts"] = 0.0


def set_enabled(enabled: bool) -> str:
    """Flip account data retention on/off. Returns the resulting mode.

    enabled=True  → ``provider_data_share`` (required by Mythos-class models).
    enabled=False → ``none`` (explicit zero retention for all models).
    """
    mode = SHARING_MODE if enabled else DEFAULT_RESTORE_MODE
    _get_control_plane_client().put_account_data_retention(mode=mode)
    # Refresh the mode cache immediately: without this, an agent spawned right
    # after the user consents reads the stale pre-toggle mode for up to the TTL
    # and is blocked with a message telling them to do what they just did.
    with _mode_lock:
        _mode_cache["mode"] = mode
        _mode_cache["ts"] = time.monotonic()
    return mode


def _reset_client_cache() -> None:
    """Test hook / region-change escape hatch."""
    with _clients_lock:
        _clients.clear()


def _error_response(e: ClientError) -> JSONResponse:
    code = e.response.get("Error", {}).get("Code", "")
    if code == "AccessDeniedException":
        return JSONResponse(
            status_code=403,
            content={
                "error": (
                    "Missing IAM permission to manage account data retention. Grant "
                    "bedrock:GetAccountDataRetention and bedrock:PutAccountDataRetention "
                    "to this identity, or set the account mode to 'provider_data_share' "
                    "manually."
                )
            },
        )
    log.error("Data retention API error: %s", e)
    return JSONResponse(
        status_code=502,
        content={"error": f"Bedrock data-retention call failed: {code or e}"},
    )


@router.get("")
async def get_data_retention():
    """Report the account's current retention mode."""
    try:
        mode = get_mode()
        return {"mode": mode, "enabled": mode == SHARING_MODE}
    except ClientError as e:
        return _error_response(e)


@router.put("")
async def put_data_retention(request: Request):
    """Enable (provider_data_share) or disable (restore prior mode) retention."""
    body = await request.json()
    enabled = bool(body.get("enabled", False))
    try:
        mode = set_enabled(enabled)
        log.info("Account data retention updated: enabled=%s mode=%s", enabled, mode)
        return {"mode": mode, "enabled": mode == SHARING_MODE}
    except ClientError as e:
        return _error_response(e)
