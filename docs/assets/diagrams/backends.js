/* Architecture - model backend selection (the chat-model decision chain) */
WSDiagram.mount("backends-diagram", {
  title: "Model backend selection",
  grid: { nodeW: 160, nodeH: 62, gapX: 44, gapY: 40 },
  groups: {
    server: { label: "Chat endpoint" }, model: { label: "Routing" },
    local: { label: "On-device" }, external: { label: "Bedrock" },
    persist: { label: "Storage" }, transport: { label: "Transport" }
  },
  nodes: [
    { id: "key", group: "server", col: 0, row: 1.5, label: "Model key", sub: "from toolbar", desc: "The chat model chosen in the toolbar (latched on the first turn). Its key alone decides the path." },
    { id: "islocal", group: "model", col: 1, row: 1.5, label: "is_local?", sub: "local_ prefix", desc: "is_local_model(): keys prefixed local_ (Gemma) route on-device with no config lookup." },
    { id: "local", group: "local", col: 2, row: 0, label: "Local runtime", sub: "Gemma, on-device", desc: "stream_local_chat on a single llama.cpp executor thread. Returns early, before compaction." },
    { id: "isopenai", group: "model", col: 2, row: 2.5, label: "is_openai?", sub: "provider check", desc: "is_openai_model(): true when chat_model_meta.provider == 'openai_bedrock'." },
    { id: "openai", group: "external", kind: "external", col: 3, row: 1.5, label: "OpenAI on Bedrock", sub: "GPT-5.x", desc: "stream_openai_chat: OpenAI Responses format over Bedrock. Returns early, before compaction." },
    { id: "anth", group: "external", kind: "external", col: 3, row: 3, label: "Anthropic path", sub: "Claude", desc: "The default path: Claude Haiku / Sonnet / Opus via Bedrock streaming and the full tool loop." },
    { id: "retry", group: "server", col: 4, row: 3, label: "Retry + compaction", sub: "bedrock_retry.py", desc: "invoke_stream_with_retry: exponential backoff on throttling, reactive compaction on PromptTooLongError." },
    { id: "cost", group: "persist", kind: "store", col: 4, row: 2, label: "Cost tracking", sub: "per turn", desc: "Each Anthropic round logs token usage and estimated USD against the session and daily caps." },
    { id: "stream", group: "transport", col: 5, row: 1.5, label: "SSE stream", sub: "text/event-stream", desc: "Every backend returns a StreamingResponse of Server-Sent Events to the SPA." }
  ],
  edges: [
    { from: "key", to: "islocal" },
    { from: "islocal", to: "local", label: "yes" },
    { from: "islocal", to: "isopenai", label: "no" },
    { from: "isopenai", to: "openai", label: "yes" },
    { from: "isopenai", to: "anth", label: "no" },
    { from: "anth", to: "retry" },
    { from: "retry", to: "cost" },
    { from: "local", to: "stream" },
    { from: "openai", to: "stream" },
    { from: "anth", to: "stream" },
    { from: "cost", to: "stream" }
  ]
});
