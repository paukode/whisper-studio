/* Architecture - chat streaming & tool orchestration (the core loop) */
WSDiagram.mount("chat-pipeline-diagram", {
  title: "Chat pipeline and tool loop",
  grid: { nodeW: 166, nodeH: 60, gapX: 52, gapY: 40 },
  groups: {
    transport: { label: "Transport" }, server: { label: "Chat pipeline" }, model: { label: "Routing" },
    tools: { label: "Tools" }, security: { label: "Approval" }, external: { label: "Bedrock" }, persist: { label: "Storage" }
  },
  nodes: [
    { id: "req", group: "transport", col: 0, row: 0, label: "POST /api/chat", sub: "SSE response", desc: "A chat turn arrives: question, history, model, session, attachments." },
    { id: "setup", group: "server", col: 0, row: 1, label: "Build the turn", sub: "prompt · memory · @files", desc: "Latch session config, assemble the system prompt (WHISPER.md + memory), resolve @file mentions, expand attachments, add index grounding." },
    { id: "route", group: "model", col: 1, row: 0.5, label: "Model routing", sub: "Anthropic / OpenAI / Gemma", desc: "Local Gemma and OpenAI-on-Bedrock return early on their own paths; otherwise the Anthropic path continues." },
    { id: "condense", group: "server", col: 1, row: 1.25, label: "Condense transcript", sub: "map-reduce if oversized", desc: "An oversized transcript is condensed to per-chunk extracts before it enters the prompt. See Transcript summarization." },
    { id: "stream", group: "server", col: 2, row: 0.5, label: "Streaming loop", sub: "up to 50 rounds", desc: "Per round: check the budget, invoke the model, stream chunks, act on the stop reason." },
    { id: "bedrock", group: "external", kind: "external", col: 3, row: 0, label: "Bedrock invoke", sub: "response stream", desc: "invoke_model_with_response_stream, wrapped in retry + reactive compaction." },
    { id: "parse", group: "server", col: 3, row: 1, label: "Parse chunks", sub: "text · tool_use · thinking", desc: "Content blocks are parsed as they stream: text, thinking, and tool_use." },
    { id: "sseOut", group: "transport", col: 4, row: 0.5, label: "SSE to client", sub: "tokens · events", desc: "Text, usage, skill, grounding, and team_progress events stream to the SPA." },
    { id: "toolbatch", group: "tools", col: 1, row: 2, label: "Execute tool batch", sub: "partition + permissions", desc: "Read-safe tools run in parallel; writes serialize. Each is permission-checked." },
    { id: "approve", group: "security", col: 2, row: 2, label: "Approval gate", sub: "nonce pause", desc: "Risky tools pause the turn and wait for a client-confirmed nonce." },
    { id: "exec", group: "tools", col: 3, row: 2, label: "Executor runs", sub: "files · git · web · code", desc: "The tool's implementation runs, sandboxed where it touches the shell." },
    { id: "persist", group: "persist", kind: "store", col: 4, row: 2, label: "Persist session", sub: "SQLite UPSERT", desc: "On end_turn, the message is added and the session is saved to SQLite." }
  ],
  edges: [
    { from: "req", to: "setup" },
    { from: "setup", to: "condense" },
    { from: "condense", to: "route" },
    { from: "route", to: "stream" },
    { from: "stream", to: "bedrock", label: "invoke" },
    { from: "bedrock", to: "parse" },
    { from: "parse", to: "sseOut", label: "text" },
    { from: "parse", to: "toolbatch", label: "tool_use" },
    { from: "toolbatch", to: "approve", label: "write" },
    { from: "toolbatch", to: "exec", label: "read" },
    { from: "approve", to: "exec", label: "approved" },
    { from: "exec", to: "stream", label: "tool_result" },
    { from: "stream", to: "persist", label: "end_turn" }
  ]
});
