"""OpenAI-on-Bedrock provider (GPT-5.5 / GPT-5.4).

These models are served ONLY through the OpenAI Responses API on the
``bedrock-mantle`` endpoint (not bedrock-runtime / Converse / InvokeModel), so
this package talks to them with the ``openai`` SDK pointed at
``https://bedrock-mantle.{region}.api.aws/openai/v1`` using a short-lived bearer
token minted from the caller's AWS credentials. The streaming adapter emits the
exact same SSE event contract every other provider uses, and tool calls reuse
the shared executor + approval pipeline, so the chat UI is provider-agnostic.
"""
