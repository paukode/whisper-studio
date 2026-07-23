"""Bedrock API retry wrapper with exponential backoff.

Handles transient errors (throttling, service unavailable) with configurable
retry limits and backoff. Non-retryable errors are raised immediately.
"""

import asyncio
import logging

from server.infrastructure.errors import (
    PromptTooLongError,
    ThrottlingError,
    WhisperAPIError,
    classify_bedrock_error,
)

log = logging.getLogger("whisper-studio")

# Retry configuration
MAX_RETRIES = 3
BASE_DELAY_MS = 500
MAX_DELAY_MS = 10_000
THROTTLE_MAX_RETRIES = 5  # More retries for throttling (429)


def _backoff_ms(attempt: int, base: int = BASE_DELAY_MS, cap: int = MAX_DELAY_MS) -> int:
    """Exponential backoff with cap: 500, 1000, 2000, 4000, ... up to cap."""
    return min(base * (2**attempt), cap)


async def invoke_with_retry(
    bedrock_client,
    *,
    call_fn,
    loop: asyncio.AbstractEventLoop,
    executor,
    on_retry=None,
) -> dict:
    """Execute a Bedrock API call with retry on transient errors.

    Args:
        bedrock_client: boto3 bedrock-runtime client (unused directly, passed to call_fn)
        call_fn: Callable that performs the actual Bedrock call. Should raise on error.
        loop: asyncio event loop for run_in_executor
        executor: ThreadPoolExecutor for blocking calls
        on_retry: Optional callback(attempt, error, delay_ms) for SSE status updates

    Returns:
        The response from call_fn on success.

    Raises:
        WhisperAPIError: On non-retryable errors or after max retries exhausted.
        PromptTooLongError: Specifically for prompt-too-long (caller handles reactive compaction).
    """
    last_error: WhisperAPIError | None = None

    for attempt in range(THROTTLE_MAX_RETRIES):
        try:
            return await loop.run_in_executor(executor, call_fn)
        except Exception as raw_error:
            classified = classify_bedrock_error(raw_error)

            # Prompt too long — always raise immediately for reactive compaction
            if isinstance(classified, PromptTooLongError):
                raise classified from raw_error

            # Non-retryable — raise immediately
            if not classified.is_retryable:
                raise classified from raw_error

            last_error = classified

            # Determine max retries for this error type
            max_for_type = (
                THROTTLE_MAX_RETRIES if isinstance(classified, ThrottlingError) else MAX_RETRIES
            )
            if attempt >= max_for_type - 1:
                break

            delay_ms = _backoff_ms(attempt)
            log.warning(
                "Bedrock %s (attempt %d/%d), retrying in %dms: %s",
                classified.error_code,
                attempt + 1,
                max_for_type,
                delay_ms,
                str(classified)[:200],
            )

            if on_retry:
                try:
                    on_retry(attempt, classified, delay_ms)
                except Exception:
                    pass

            await asyncio.sleep(delay_ms / 1000.0)

    # Exhausted retries
    assert last_error is not None
    last_error.user_message = f"Failed after {attempt + 1} retries: {last_error.user_message}"
    raise last_error


async def invoke_stream_with_retry(
    bedrock_client,
    *,
    call_fn,
    loop: asyncio.AbstractEventLoop,
    executor,
    on_retry=None,
) -> dict:
    """Same as invoke_with_retry but for invoke_model_with_response_stream."""
    return await invoke_with_retry(
        bedrock_client,
        call_fn=call_fn,
        loop=loop,
        executor=executor,
        on_retry=on_retry,
    )
