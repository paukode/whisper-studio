"""An AccessDeniedException reaches the user with Bedrock's own reason."""

from server.infrastructure.errors import ModelNotAvailableError, classify_bedrock_error


def test_access_denied_names_the_bedrock_reason():
    err = classify_bedrock_error(
        Exception(
            "An error occurred (AccessDeniedException) when calling the "
            "InvokeModelWithResponseStream operation: You don't have access to the "
            "model with the specified model ID."
        )
    )
    assert isinstance(err, ModelNotAvailableError)
    assert "You don't have access to the model with the specified model ID." in err.user_message
    assert "''" not in err.user_message
    assert "IAM permissions" in err.user_message
    assert err.is_retryable is False
