"""A cloud turn with no AWS credentials ends with a next step, not botocore's text.

botocore raises NoCredentialsError (or PartialCredentialsError) before any
request is sent. Classified as a generic API error, the chat showed "API error:
Unable to locate credentials" and nothing else. The classifier matches these by
type, so the existing error frame tells the user how to set credentials up or
that Local mode chats without AWS.
"""

from botocore.exceptions import NoCredentialsError, PartialCredentialsError

from server.infrastructure.errors import classify_bedrock_error
from tests.golden_harness import FakeBedrockClient, run_chat_turn


def test_missing_and_partial_credentials_are_classified_with_a_next_step():
    for raw in (
        NoCredentialsError(),
        PartialCredentialsError(provider="env", cred_var="AWS_SECRET_ACCESS_KEY"),
    ):
        err = classify_bedrock_error(raw)
        assert err.error_code == "NO_AWS_CREDENTIALS"
        assert not err.is_retryable
        assert "aws configure" in err.user_message
        assert "Local" in err.user_message
        assert not err.user_message.startswith("API error")


def test_a_message_that_merely_mentions_credentials_is_not_misclassified():
    err = classify_bedrock_error(RuntimeError("Unable to locate credentials"))
    assert err.error_code == "API_ERROR"


class _NoCredentialsClient(FakeBedrockClient):
    def invoke_model_with_response_stream(self, **_kw):
        self.requests.append({})
        raise NoCredentialsError()


def test_a_turn_without_credentials_ends_with_the_actionable_error_frame(monkeypatch):
    client = _NoCredentialsClient([])
    lines = run_chat_turn(monkeypatch, client, {"question": "hey"})
    joined = "\n".join(lines)

    assert lines[-1] == "[DONE]"
    assert "aws configure" in joined
    assert "Unable to locate credentials" not in joined
    # Not retried: a missing credential never fixes itself between attempts.
    assert len(client.requests) == 1
