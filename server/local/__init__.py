"""On-device (local mode) backend — fully isolated from the cloud/Bedrock path.

Everything specific to running local models lives under this package:
  - registry.py      — the config-driven model registry (add a model in config.json)
  - llama_server.py  — the llama-server subprocess manager (GGUF engine)
  - mlx_server.py    — the mlx_lm server subprocess manager (MLX engine)
  - serving.py       — engine dispatch: one facade the rest of the app calls
  - runtime.py       — model metadata, downloads, capability flags, local prompt
  - server_stream.py — the SSE adapter + agentic tool loop for a local turn
  - route.py         — the chat-endpoint bridge (local vs cloud, fresh/resume)
  - tools/           — tool-pool declarations + the shared safety gate

There are exactly two on-device engines and no in-process fallback: GGUF models
are served through upstream llama.cpp's own chat template and tool-call parsers,
MLX models (``"engine": "mlx"`` in the entry) through mlx_lm's server, and an
unusable engine fails loudly rather than silently degrading to a model-specific
code path. Both speak the same OpenAI-compatible SSE, so everything above the
serving facade is engine-agnostic. One local model is resident at a time ACROSS
both engines (serving.ensure_serving stops the other engine first).

The cloud Claude path (server/chat) never imports from here except at the one
branch point in server/chat/routes.py that calls route.local_chat_response().
Tool execution reuses the shared executor + approval pipeline (server/
tool_executor.py) — nothing is copied; destructive tools stay gated.
"""
