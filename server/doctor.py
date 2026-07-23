"""
Diagnostics endpoint — checks AWS credentials, Bedrock connectivity,
workspace state, and sessions DB.
"""

import asyncio
import json
import logging
import os
import sqlite3

from fastapi import APIRouter

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/doctor", tags=["doctor"])


@router.get("")
async def doctor(model: str = None):
    results = []

    # ── 1. AWS credentials ────────────────────────────────────────────────────
    has_env = bool(os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"))
    creds_file = os.path.expanduser("~/.aws/credentials")
    has_file = os.path.isfile(creds_file)
    has_profile = bool(os.environ.get("AWS_PROFILE") or os.environ.get("AWS_DEFAULT_PROFILE"))
    if has_env or has_file or has_profile:
        detail = []
        if has_env:
            detail.append("env vars")
        if has_file:
            detail.append("~/.aws/credentials")
        if has_profile:
            detail.append(
                f"profile={os.environ.get('AWS_PROFILE') or os.environ.get('AWS_DEFAULT_PROFILE')}"
            )
        results.append({"check": "AWS credentials", "status": "ok", "detail": ", ".join(detail)})
    else:
        results.append(
            {
                "check": "AWS credentials",
                "status": "error",
                "detail": "No AWS credentials found. Set AWS_ACCESS_KEY_ID/SECRET or configure ~/.aws/credentials",
            }
        )

    # ── 2. Bedrock connectivity ───────────────────────────────────────────────
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
            bedrock.invoke_model(
                modelId=ping_model,
                contentType="application/json",
                accept="application/json",
                body=body,
            )

        await asyncio.get_running_loop().run_in_executor(None, _invoke)
        results.append(
            {
                "check": "Bedrock connectivity",
                "status": "ok",
                "detail": f"region={region}, model={ping_model}",
            }
        )
    except Exception as e:
        results.append({"check": "Bedrock connectivity", "status": "error", "detail": str(e)})

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
        from server.infrastructure.config import load_config

        cfg = load_config()
        effort = cfg.get("effort_level", "high")
        model = cfg.get("default_chat_model", "opus4.6")
        results.append(
            {
                "check": "Config",
                "status": "ok",
                "detail": f"model={model}, effort={effort}, region={cfg.get('bedrock_region', 'us-east-1')}",
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
