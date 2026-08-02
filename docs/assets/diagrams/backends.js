/* Architecture - model backend selection (the chat-model decision chain) */
WSDiagram.mount("backends-diagram", {
  title: "Model backend selection",
  grid: { nodeW: 160, nodeH: 62, gapX: 44, gapY: 40 },
  groups: {
    server: { label: "Chat endpoint" }, model: { label: "Routing" },
    local: { label: "On-device" }, external: { label: "Adapters" },
    engine: { label: "Turn engine" }, persist: { label: "Storage" },
    transport: { label: "Transport" }
  },
  nodes: [
    { id: "key", group: "server", col: 0, row: 1.5, label: "Model key", sub: "from toolbar", desc: "The chat model chosen in the toolbar (latched on the first turn). Its key alone decides the adapter." },
    { id: "islocal", group: "model", col: 1, row: 1.5, label: "is_local?", sub: "registry member", desc: "is_local_model(): the key is in the on-device registry (built-ins merged with is_local config entries)." },
    { id: "local", group: "local", col: 2, row: 0, label: "LocalAdapter", sub: "llama-server", desc: "server/chat/engine/local.py: lean prompt, llama-server subprocess, tool-call fidelity, offline LOCAL policy (no completion gate, no permission-explainer model)." },
    { id: "isopenai", group: "model", col: 2, row: 2.5, label: "provider?", sub: "chat_model_meta", desc: "provider == 'openai_bedrock' selects the Responses adapter; everything else is Anthropic." },
    { id: "openai", group: "external", kind: "external", col: 3, row: 1.5, label: "OpenAI adapter", sub: "GPT-5.x on mantle", desc: "server/chat/engine/openai.py: Responses API over bedrock-mantle, early-release + heartbeat quirks, prompt-too-long detection against the model's real window." },
    { id: "anth", group: "external", kind: "external", col: 3, row: 3, label: "Anthropic adapter", sub: "Claude on Bedrock", desc: "server/chat/engine/anthropic.py: Bedrock streaming, prompt-cache checkpoints + canary, invoke retry with exponential backoff." },
    { id: "engine", group: "engine", col: 4, row: 1.5, label: "Turn engine", sub: "engine/runner.py", desc: "ONE loop for every provider: rounds + wind-down, proactive compaction, prompt-too-long rescue, the salvage round, tool execution through the shared safety gate, approval pauses, completion gate." },
    { id: "cost", group: "persist", kind: "store", col: 5, row: 2.5, label: "Cost tracking", sub: "every provider", desc: "The engine logs each round's token usage and estimated USD against the session and daily caps - Claude, GPT, and local alike." },
    { id: "stream", group: "transport", col: 5, row: 0.5, label: "SSE stream", sub: "text/event-stream", desc: "The engine yields one shared frame vocabulary as a StreamingResponse of Server-Sent Events." }
  ],
  edges: [
    { from: "key", to: "islocal" },
    { from: "islocal", to: "local", label: "yes" },
    { from: "islocal", to: "isopenai", label: "no" },
    { from: "isopenai", to: "openai", label: "gpt" },
    { from: "isopenai", to: "anth", label: "claude" },
    { from: "local", to: "engine" },
    { from: "openai", to: "engine" },
    { from: "anth", to: "engine" },
    { from: "engine", to: "cost" },
    { from: "engine", to: "stream" }
  ]
});
