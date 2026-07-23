"""On-device (local mode) backend — fully isolated from the cloud/Bedrock path.

Everything specific to running local GGUF models lives under this package:
  - runtime.py — the llama-cpp-python runtime (load/unload, chat, thinking)
  - stream.py  — the SSE adapter + agentic tool loop that bridges a local turn
  - route.py   — the chat-endpoint bridge (decides local vs cloud, fresh/resume)
  - tools/     — full tool pool glue + Gemma function-calling adapters

The cloud Claude path (server/chat) never imports from here except at the one
branch point in server/chat/routes.py that calls route.local_chat_response().
Tool execution reuses the shared executor + approval pipeline (server/
tool_executor.py) — nothing is copied; destructive tools stay gated.
"""
