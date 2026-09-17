"""Thin async wrapper over the Bedrock ``InvokeModelWithBidirectionalStream``
duplex stream, the only API Nova Sonic speaks.

boto3 has no bidirectional-stream method, so this uses the Smithy-based
``aws-sdk-bedrock-runtime`` package (Developer Preview, Python 3.12+). The import
is lazy: everything else in server/voice loads without it, and
``sdk_availability()`` tells the UI why voice mode is off when it is missing.

Credentials come from botocore's default chain (env, shared config, profiles,
SSO), so voice mode authenticates exactly like the rest of the app's Bedrock
calls. Bedrock API keys (``AWS_BEARER_TOKEN_BEDROCK``) are not accepted by this
API; SigV4 is required.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_MIN_PY = (3, 12)


class SonicUnavailable(RuntimeError):
    """Voice mode cannot start on this machine/account."""


class SonicStreamError(RuntimeError):
    """The model stream reported an in-band error (throttling, validation, ...)."""


def sdk_availability() -> tuple[bool, str | None]:
    """(available, reason). Never raises."""
    import sys

    if sys.version_info < _MIN_PY:
        return False, (
            f"Voice mode needs Python {_MIN_PY[0]}.{_MIN_PY[1]}+ for the Bedrock streaming "
            f"SDK (running {sys.version_info.major}.{sys.version_info.minor})."
        )
    try:
        import aws_sdk_bedrock_runtime.client  # noqa: F401
    except ImportError:
        return False, (
            "The Bedrock streaming SDK is not installed. Run "
            "`pip install 'aws-sdk-bedrock-runtime[awscrt]'` in the app's Python environment."
        )
    try:
        from smithy_http.aio.crt import AWSCRTHTTPClient  # noqa: F401
    except ImportError:
        return False, (
            "The Bedrock streaming SDK needs its awscrt transport for bidirectional "
            "streams. Run `pip install 'aws-sdk-bedrock-runtime[awscrt]'`."
        )
    return True, None


def _frozen_credentials():
    """Resolve credentials through botocore's default chain, once per stream."""
    import botocore.session

    creds = botocore.session.get_session().get_credentials()
    if creds is None:
        raise SonicUnavailable(
            "No AWS credentials found. Voice mode uses the same AWS credentials as chat "
            "(aws configure, AWS_PROFILE or SSO)."
        )
    return creds.get_frozen_credentials()


async def _build_client(region: str):
    from aws_sdk_bedrock_runtime.client import AsyncBedrockRuntimeClient
    from aws_sdk_bedrock_runtime.config import AsyncBedrockRuntimeConfig
    from smithy_aws_core.identity import AWSCredentialsIdentity, StaticCredentialsResolver
    from smithy_http.aio.crt import AWSCRTHTTPClient

    frozen = _frozen_credentials()
    identity = AWSCredentialsIdentity(
        access_key_id=frozen.access_key,
        secret_access_key=frozen.secret_key,
        session_token=frozen.token,
    )
    # The SDK's config is async-resolved (env, shared config, defaults); the
    # explicit overrides pin the region and the credentials we already hold.
    config = await AsyncBedrockRuntimeConfig.resolve(
        region=region,
        endpoint_uri=f"https://bedrock-runtime.{region}.amazonaws.com",
        aws_credentials_identity_resolver=StaticCredentialsResolver(identity=identity),
        # The default aiohttp transport cannot do duplex streaming; the CRT
        # HTTP/2 client can (SUPPORTS_DUPLEX_STREAMING).
        transport=AWSCRTHTTPClient(),
    )
    return AsyncBedrockRuntimeClient(config=config)


class SonicStream:
    """One open bidirectional stream. ``send`` takes the JSON strings built by
    protocol.py; ``receive`` yields raw JSON bytes from the model (or ``None``
    when the model closed the stream)."""

    def __init__(self, client: Any, duplex: Any) -> None:
        self._client = client
        self._duplex = duplex
        self._output = None
        self._closed = False

    async def send(self, event_json: str) -> None:
        if self._closed:
            return
        from aws_sdk_bedrock_runtime.models import (
            BidirectionalInputPayloadPart,
            InvokeModelWithBidirectionalStreamInputChunk,
        )

        chunk = InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(bytes_=event_json.encode("utf-8"))
        )
        await self._duplex.input_stream.send(chunk)

    async def receive(self) -> bytes | None:
        if self._closed:
            return None
        if self._output is None:
            _, self._output = await self._duplex.await_output()
        event = await self._output.receive()
        if event is None:
            return None
        value = getattr(event, "value", None)
        payload = getattr(value, "bytes_", None)
        if payload is not None:
            return payload
        # Every non-chunk union member is an exception shape; surface it.
        raise SonicStreamError(_describe_error(event))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for closer in (self._duplex.close, self._client.close):
            try:
                await closer()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass


def _describe_error(event: Any) -> str:
    value = getattr(event, "value", event)
    message = getattr(value, "message", None) or str(value)
    name = type(value).__name__
    return f"{name}: {message}"


async def open_stream(model_id: str, region: str) -> SonicStream:
    """Open the duplex stream. Raises ``SonicUnavailable`` for missing SDK or
    credentials; provider-side failures surface on the first ``receive``."""
    ok, reason = sdk_availability()
    if not ok:
        raise SonicUnavailable(reason or "voice mode unavailable")
    from aws_sdk_bedrock_runtime.models import InvokeModelWithBidirectionalStreamOperationInput

    client = await _build_client(region)
    try:
        duplex = await client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(model_id=model_id)
        )
    except Exception:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass
        raise
    return SonicStream(client, duplex)
