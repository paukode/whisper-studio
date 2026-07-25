"""On-device (local mode) backend — fully isolated from the cloud/Bedrock path.

Everything specific to running local GGUF models lives under this package:
  - registry.py      — the config-driven model registry (add a model in config.json)
  - llama_server.py  — the llama-server subprocess manager (the one engine)
  - runtime.py       — model metadata, downloads, capability flags, local prompt
  - server_stream.py — the SSE adapter + agentic tool loop for a local turn
  - route.py         — the chat-endpoint bridge (local vs cloud, fresh/resume)
  - tools/           — tool-pool declarations + the shared safety gate

llama-server is the ONLY on-device engine: there is no in-process fallback, so a
model added to config.json is served through upstream llama.cpp's own chat
template and tool-call parsers, and an unusable binary fails loudly rather than
silently degrading to a Gemma-specific code path.

The cloud Claude path (server/chat) never imports from here except at the one
branch point in server/chat/routes.py that calls route.local_chat_response().
Tool execution reuses the shared executor + approval pipeline (server/
tool_executor.py) — nothing is copied; destructive tools stay gated.
"""
