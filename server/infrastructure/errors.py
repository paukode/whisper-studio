"""Typed error classes for Whisper Studio.

Provides structured errors with error codes, user-friendly messages,
and retry classification for the error handling pipeline.
"""


class WhisperError(Exception):
    """Base error class for all Whisper errors."""

    def __init__(self, message: str, *, error_code: str = "UNKNOWN", user_message: str = ""):
        super().__init__(message)
        self.error_code = error_code
        self.user_message = user_message or message


class WhisperAPIError(WhisperError):
    """Error from Bedrock/API calls. Carries retry classification."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "API_ERROR",
        user_message: str = "",
        is_retryable: bool = False,
        status_code: int = 0,
        original_error: Exception | None = None,
    ):
        super().__init__(message, error_code=error_code, user_message=user_message)
        self.is_retryable = is_retryable
        self.status_code = status_code
        self.original_error = original_error


class WhisperToolError(WhisperError):
    """Error during tool execution."""

    def __init__(
        self,
        message: str,
        *,
        tool_name: str = "",
        error_code: str = "TOOL_ERROR",
        user_message: str = "",
    ):
        super().__init__(message, error_code=error_code, user_message=user_message)
        self.tool_name = tool_name


class WhisperConfigError(WhisperError):
    """Error in configuration loading or validation."""

    def __init__(
        self,
        message: str,
        *,
        config_path: str = "",
        error_code: str = "CONFIG_ERROR",
        user_message: str = "",
    ):
        super().__init__(message, error_code=error_code, user_message=user_message)
        self.config_path = config_path


class PromptTooLongError(WhisperAPIError):
    """Bedrock rejected the request because the prompt exceeds the context window."""

    def __init__(self, message: str, *, token_count: int = 0, token_limit: int = 0):
        super().__init__(
            message,
            error_code="PROMPT_TOO_LONG",
            user_message="The conversation is too long. Compacting context...",
            is_retryable=True,
        )
        self.token_count = token_count
        self.token_limit = token_limit


class ThrottlingError(WhisperAPIError):
    """Bedrock rate limit / throttling (429 equivalent)."""

    def __init__(self, message: str):
        super().__init__(
            message,
            error_code="THROTTLING",
            user_message="Model is busy, retrying...",
            is_retryable=True,
        )


class ServiceUnavailableError(WhisperAPIError):
    """Bedrock service unavailable (503 equivalent)."""

    def __init__(self, message: str):
        super().__init__(
            message,
            error_code="SERVICE_UNAVAILABLE",
            user_message="Service temporarily unavailable, retrying...",
            is_retryable=True,
        )


class ModelNotAvailableError(WhisperAPIError):
    """Requested model is not available in the region or account."""

    def __init__(self, message: str, *, model_id: str = ""):
        super().__init__(
            message,
            error_code="MODEL_NOT_AVAILABLE",
            user_message=f"Model '{model_id}' is not available. Check region and model access.",
            is_retryable=False,
        )
        self.model_id = model_id


class DataRetentionRequiredError(WhisperAPIError):
    """Model requires the account's Bedrock data-retention mode to be
    ``provider_data_share`` (Mythos-class models like Claude Fable 5). Bedrock
    rejects these with "data retention mode 'default' is not available for this
    model" until the account opts in."""

    def __init__(self, message: str):
        super().__init__(
            message,
            error_code="DATA_RETENTION_REQUIRED",
            user_message=(
                "This model requires enabling Bedrock data retention for your AWS "
                "account (Mythos-class models like Fable 5). Re-select the model in "
                "the UI to enable it, or set the account data-retention mode to "
                "'provider_data_share' (bedrock:PutAccountDataRetention)."
            ),
            is_retryable=False,
        )


# ── Error classification ─────────────────────────────────────────────


def classify_bedrock_error(error: Exception) -> WhisperAPIError:
    """Classify a raw Bedrock/botocore exception into a typed WhisperAPIError."""
    error_str = str(error)
    error_code = getattr(error, "response", {}).get("Error", {}).get("Code", "")

    if error_code == "ThrottlingException" or "ThrottlingException" in error_str:
        return ThrottlingError(error_str)

    if error_code == "ServiceUnavailableException" or "ServiceUnavailable" in error_str:
        return ServiceUnavailableError(error_str)

    if "too long" in error_str.lower() or "prompt is too long" in error_str.lower():
        return PromptTooLongError(error_str)

    if "AccessDeniedException" in error_str or error_code == "AccessDeniedException":
        return ModelNotAvailableError(error_str)

    # Mythos-class models reject the default retention mode. Check before the
    # generic ValidationException branch so the user gets an actionable message.
    if "data retention" in error_str.lower():
        return DataRetentionRequiredError(error_str)

    if "ValidationException" in error_str:
        # Surface the actual Bedrock reason (e.g. "tool_choice may not be used
        # with thinking") instead of a generic label. These are almost always
        # actionable request-shape problems, and hiding the detail forces a
        # server-log dig for every mismatch. Botocore formats the message as
        # "... operation: <reason>", so keep the reason when present.
        detail = (
            error_str.split("operation:", 1)[1].strip() if "operation:" in error_str else error_str
        )
        return WhisperAPIError(
            error_str,
            error_code="VALIDATION_ERROR",
            user_message=f"Invalid request sent to model: {detail[:300]}",
            is_retryable=False,
            original_error=error,
        )

    # Default: non-retryable API error
    return WhisperAPIError(
        error_str,
        error_code="API_ERROR",
        user_message=f"API error: {error_str[:200]}",
        is_retryable=False,
        original_error=error,
    )
