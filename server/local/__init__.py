"""On-device backend: everything specific to running local models.

  - registry.py      the config-driven model registry (chat_models entries in
                     config.user.json)
  - llama_server.py  the llama-server subprocess manager (GGUF engine)
  - mlx_server.py    the mlx_lm server subprocess manager (MLX engine)
  - serving.py       engine dispatch: one facade the rest of the app calls
  - runtime.py       model metadata, downloads, capability flags, local prompt
  - thinking.py      thinking levels read from each model's chat template
  - server_stream.py the SSE adapter and the stream of one local round
  - route.py         the chat-endpoint bridge (local vs cloud, fresh/resume)
  - tools/           the OpenAI function-shape converter for tool schemas

There are exactly two on-device engines and no in-process fallback: GGUF models
are served through upstream llama.cpp's own chat template and tool-call parsers,
MLX models (``"engine": "mlx"`` in the entry) through mlx_lm's server, and an
unusable engine fails loudly rather than silently degrading to a model-specific
code path. Both speak the same OpenAI-compatible SSE, so everything above the
serving facade is engine-agnostic. One local model is resident at a time ACROSS
both engines (serving.ensure_serving stops the other engine first).

server/chat reaches in where a turn needs local facts (the route branch in
server/chat/routes.py, the local engine adapter, context windows, compaction,
refusals). Tool execution reuses the shared executor and approval pipeline
(server/tool_executor.py): nothing is copied, and destructive tools stay gated.
"""
