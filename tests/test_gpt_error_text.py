"""GPT error text names the real cause: a token limit or a context size that
contains numbers such as 401 or 403 is not a credentials failure."""

from server.chat.engine.openai import _friendly_error


class _StatusError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def test_an_output_token_limit_with_403_in_it_is_not_reported_as_auth():
    text = _friendly_error(
        _StatusError("Error code: 400 - max_output_tokens must be at most 403 for this model", 400)
    )

    assert "auth" not in text.lower()
    assert text.startswith("OpenAI (Bedrock) request failed:")


def test_a_token_count_that_looks_like_401_is_not_a_status_code():
    text = _friendly_error(Exception("context has 401 tokens over the 128000 limit"))

    assert "auth" not in text.lower()


def test_a_real_403_is_reported_as_auth():
    text = _friendly_error(_StatusError("Forbidden", 403))

    assert text.startswith("GPT auth/access failed.")


def test_access_denied_text_without_a_status_is_reported_as_auth():
    text = _friendly_error(Exception("AccessDeniedException: not authorized to invoke"))

    assert text.startswith("GPT auth/access failed.")
