/* Architecture - anatomy of a single chat turn (the request sequence) */
WSDiagram.mount("chat-turn-diagram", {
  title: "One chat turn, keystroke to persisted message",
  grid: { nodeW: 156, nodeH: 60, gapX: 46, gapY: 42 },
  groups: {
    browser: { label: "Browser" }, transport: { label: "Transport" },
    server: { label: "Chat pipeline" }, external: { label: "Bedrock" },
    security: { label: "Approval" }, tools: { label: "Tools" },
    persist: { label: "Storage" }
  },
  nodes: [
    { id: "user", group: "browser", col: 0, row: 0, label: "User types + Send", desc: "The SPA adds the user bubble, marks the session streaming, and opens a fetch to the chat endpoint." },
    { id: "post", group: "transport", col: 1, row: 0, label: "POST /api/chat", sub: "SSE response", desc: "Body carries question, capped history, model, effort, session_id, and attachment_ids. The response is a Server-Sent Events stream, not one JSON body." },
    { id: "build", group: "server", col: 2, row: 0, label: "Build prompt", sub: "history · WHISPER.md · memory", desc: "The endpoint filters visible_chat_history (drops cron_event rows), latches the session config, and assembles the system prompt from workspace context, WHISPER.md, and memory." },
    { id: "invoke", group: "external", kind: "external", col: 3, row: 0, label: "Invoke Bedrock", sub: "streaming", desc: "invoke_model_with_response_stream opens the upstream SSE. Content blocks arrive as they are generated." },
    { id: "text", group: "server", col: 4, row: 0, label: "Stream tokens", desc: "Each text chunk is yielded to the client as a token; the SPA appends it to the live bubble." },
    { id: "done", group: "transport", col: 5, row: 0, label: "[DONE]", desc: "The stream terminates. The SPA commits the final assistant message atomically with clearing the streaming state." },
    { id: "tooluse", group: "server", col: 4, row: 1, label: "tool_use?", sub: "stop reason", desc: "A tool_use stop reason means the model wants to act. The endpoint routes each call before continuing." },
    { id: "approve", group: "security", col: 3, row: 2, label: "Approval?", sub: "nonce", desc: "A tool that needs consent pauses the turn, stashes state in _paused_sessions, issues a server-held nonce, and emits an approval_request over SSE." },
    { id: "exec", group: "tools", col: 4, row: 2, label: "Execute tool", desc: "The validated action runs via its executor. The tool_result is fed back so the model can continue." },
    { id: "persist", group: "persist", kind: "store", col: 5, row: 1, label: "Persist history", sub: "PUT /api/sessions", desc: "The SPA PUTs the session; the server UPSERTs chat_history under a per-session lock + merge so background cron rows are not clobbered." }
  ],
  edges: [
    { from: "user", to: "post" },
    { from: "post", to: "build" },
    { from: "build", to: "invoke" },
    { from: "invoke", to: "text" },
    { from: "text", to: "done", label: "end_turn" },
    { from: "invoke", to: "tooluse" },
    { from: "tooluse", to: "approve", label: "needs consent" },
    { from: "approve", to: "exec", label: "approved" },
    { from: "tooluse", to: "exec", label: "allow-listed" },
    { from: "exec", to: "invoke", label: "tool_result" },
    { from: "done", to: "persist" }
  ]
});
