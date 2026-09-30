"""
Diagnostics endpoint: checks AWS credentials, Bedrock connectivity, the
regions the GPT models resolve to, workspace state, and sessions DB. In Local
mode the AWS and GPT rows are skipped, since that mode makes no AWS calls.
"""

import asyncio
import json
import logging
import os
import sqlite3

from fastapi import APIRouter

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/doctor", tags=["doctor"])

_LOCAL_MODE_SKIP = "Skipped: Local mode makes no AWS calls."
_CREDS = "AWS credentials"
_BEDROCK = "Bedrock connectivity"
_GPT_REGIONS = "GPT regions"


def _gpt_region_check() -> dict:
    """Warn when a GPT model the picker offers resolves to a region where
    bedrock-mantle does not serve it (an EU bedrock_region, say): its first
    turn would fail with a 404. Nothing is rerouted; this only says what to
    set. The region is resolved exactly as a call resolves it."""
    from server.chat.infra import mode_chat_catalog
    from server.infrastructure.config import load_config
    from server.openai_bedrock.runtime import (
        gpt_regions_note,
        region_problems,
        unmeasured_models,
    )
    from server.workspace import get_workspace_path

    visible, meta, _mode, _default = mode_chat_catalog(load_config(get_workspace_path()))
    gpt = [k for k in visible if (meta.get(k) or {}).get("provider") == "openai_bedrock"]
    if not gpt:
        return {"check": _GPT_REGIONS, "status": "ok", "detail": "No GPT model configured"}
    problems = region_problems(gpt)
    if not problems:
        unknown = unmeasured_models(gpt)
        detail = f"{len(gpt) - len(unknown)} GPT model(s), each in a region Bedrock serves it in"
        if unknown:
            detail += f"; no measured regions for {', '.join(unknown)}"
        return {"check": _GPT_REGIONS, "status": "ok", "detail": detail}
    return {
        "check": _GPT_REGIONS,
        "status": "warn",
        "detail": " ".join([*problems, gpt_regions_note()]),
    }


async def _bedrock_check(model: str | None) -> dict:
    try:
        import boto3

        from server.infrastructure.config import load_config

        cfg = load_config()
        region = cfg.get("bedrock_region", "us-east-1")
        bedrock = boto3.client("bedrock-runtime", region_name=region)
        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "ping"}],
            }
        )
        from server.chat import _get_chat_models

        chat_models = _get_chat_models()
        ping_model = (
            (chat_models.get(model) if model else None)
            or chat_models.get("sonnet")
            or chat_models.get("opus4.6")
            or next(iter(chat_models.values()))
        )

        def _invoke():
            from server.costs.calls import invoke_claude

            invoke_claude(
                bedrock,
                model_id=ping_model,
                contentType="application/json",
                accept="application/json",
                body=body,
                source="doctor",
            )

        await asyncio.get_running_loop().run_in_executor(None, _invoke)
        return {"check": _BEDROCK, "status": "ok", "detail": f"region={region}, model={ping_model}"}
    except Exception as e:
        return {"check": _BEDROCK, "status": "error", "detail": str(e)}


def _credential_source() -> str | None:
    """How AWS credentials resolve through botocore's real provider chain (the
    chain every client uses), or None when nothing resolves. Refreshable
    credentials (SSO, assume-role, credential_process) are frozen here so an
    expired SSO session shows up in this row, not on the first turn. Blocking
    (subprocesses, the SSO cache, metadata lookups): call it off the loop."""
    from server.infrastructure.aws_clients import bedrock_api_key_present, resolve_credentials

    creds = resolve_credentials()
    if creds is not None:
        creds.get_frozen_credentials()
        return getattr(creds, "method", "") or "provider chain"
    if bedrock_api_key_present():
        return "Bedrock API key (AWS_BEARER_TOKEN_BEDROCK)"
    return None


async def _credentials_check() -> dict:
    try:
        source = await asyncio.get_running_loop().run_in_executor(None, _credential_source)
    except Exception as e:  # noqa: BLE001 - a broken profile is the diagnosis
        return {
            "check": _CREDS,
            "status": "error",
            "detail": f"Could not load AWS credentials: {e}",
        }
    if source is None:
        return {
            "check": _CREDS,
            "status": "error",
            "detail": (
                "No AWS credentials found. Run `aws configure`, or `aws sso login` for an "
                "SSO profile in ~/.aws/config."
            ),
        }
    return {"check": _CREDS, "status": "ok", "detail": f"resolved via {source}"}


@router.get("")
async def doctor(model: str = None):
    results = []

    from server.infrastructure.cloud_guard import cloud_allowed

    local_mode = not cloud_allowed()

    # ── 1. AWS credentials ────────────────────────────────────────────────────
    if local_mode:
        results.append({"check": _CREDS, "status": "ok", "detail": _LOCAL_MODE_SKIP})
    else:
        results.append(await _credentials_check())

    # ── 2. Bedrock connectivity ───────────────────────────────────────────────
    if local_mode:
        results.append({"check": _BEDROCK, "status": "ok", "detail": _LOCAL_MODE_SKIP})
    else:
        results.append(await _bedrock_check(model))

    # ── 2b. GPT regions (config only, no AWS call) ────────────────────────────
    if local_mode:
        results.append({"check": _GPT_REGIONS, "status": "ok", "detail": _LOCAL_MODE_SKIP})
    else:
        try:
            results.append(_gpt_region_check())
        except Exception as e:  # noqa: BLE001 - a diagnostics row must not 500
            results.append({"check": _GPT_REGIONS, "status": "error", "detail": str(e)})

    # ── 3. Workspace ──────────────────────────────────────────────────────────
    try:
        from server.workspace import get_workspace_path

        ws = get_workspace_path()
        if ws and os.path.isdir(ws):
            results.append({"check": "Workspace", "status": "ok", "detail": ws})
        elif ws:
            results.append(
                {
                    "check": "Workspace",
                    "status": "warn",
                    "detail": f"Configured path does not exist: {ws}",
                }
            )
        else:
            results.append(
                {"check": "Workspace", "status": "warn", "detail": "No workspace connected"}
            )
    except Exception as e:
        results.append({"check": "Workspace", "status": "error", "detail": str(e)})

    # ── 4. Sessions database ──────────────────────────────────────────────────
    try:
        from server.infrastructure.sessions import DB_PATH

        if os.path.isfile(DB_PATH):
            conn = sqlite3.connect(DB_PATH)
            count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            conn.close()
            results.append(
                {
                    "check": "Sessions DB",
                    "status": "ok",
                    "detail": f"{count} session(s) stored at {DB_PATH}",
                }
            )
        else:
            results.append(
                {
                    "check": "Sessions DB",
                    "status": "warn",
                    "detail": "Database not yet created (no sessions saved)",
                }
            )
    except Exception as e:
        results.append({"check": "Sessions DB", "status": "error", "detail": str(e)})

    # ── 5. Config ─────────────────────────────────────────────────────────────
    try:
        from server.chat.infra import mode_chat_catalog
        from server.infrastructure.config import load_config
        from server.infrastructure.model_mode import NO_LOCAL_MODEL_REASON
        from server.workspace import get_workspace_path

        # The model a turn would run on: the active mode's default, resolved
        # exactly as /api/models resolves it (not the raw config key, which
        # names a cloud model even in Local mode).
        cfg = load_config(get_workspace_path())
        _visible, _meta, mode, model = mode_chat_catalog(cfg)
        effort = cfg.get("effort_level", "high")
        if not model:
            reason = (
                NO_LOCAL_MODEL_REASON
                if mode == "local"
                else "No chat model is available. Enable one in Settings > Models."
            )
            results.append(
                {"check": "Config", "status": "warn", "detail": f"mode={mode}: {reason}"}
            )
        else:
            # A Bedrock region means nothing in Local mode.
            region = "" if mode == "local" else f", region={cfg.get('bedrock_region', 'us-east-1')}"
            results.append(
                {
                    "check": "Config",
                    "status": "ok",
                    "detail": f"mode={mode}, model={model}, effort={effort}{region}",
                }
            )
    except Exception as e:
        results.append({"check": "Config", "status": "error", "detail": str(e)})

    overall = (
        "ok"
        if all(r["status"] == "ok" for r in results)
        else ("error" if any(r["status"] == "error" for r in results) else "warn")
    )
    return {"status": overall, "checks": results}


@router.get("/context")
async def doctor_context(question: str = ""):
    """Rightsize the prompt: per-section cost, and what is being paid for twice.

    The credentials/connectivity checks above catch things that fail loudly.
    This catches the failures that don't: a section duplicating a tool
    description, static text stranded on a per-request layer, or an instruction
    naming a tool that no longer exists.
    """
    from server.context_report import build_context_report
    from server.workspace import get_workspace_path

    try:
        return build_context_report(ws_path=get_workspace_path(), question=question)
    except Exception as e:  # noqa: BLE001 — a diagnostics endpoint must not 500
        log.warning("context report failed: %s", e)
        return {"error": str(e)}
