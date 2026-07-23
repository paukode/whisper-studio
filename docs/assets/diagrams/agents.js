/* Architecture - the agent runtime, mailbox, and per-session event bus */
WSDiagram.mount("agents-diagram", {
  title: "Agent runtime and the event bus",
  grid: { nodeW: 172, nodeH: 60, gapX: 56, gapY: 46 },
  groups: {
    server: { label: "Chat turn" }, agents: { label: "Agent runtime" },
    tools: { label: "Tools" }, transport: { label: "Transport" }, browser: { label: "SPA" }
  },
  nodes: [
    { id: "chat", group: "server", col: 0, row: 0, label: "Chat turn", sub: "spawn_agent / team_create", desc: "A tool call in the main /api/chat loop fans out to the agent runtime via spawn_agent or team_create." },
    { id: "runtime", group: "agents", col: 1, row: 0, label: "Agent runtime", sub: "run_agent loop", desc: "run_agent runs a non-streaming invoke_model loop up to config.max_turns, one agent id per agent." },
    { id: "tools", group: "tools", col: 2, row: 0, label: "execute_tool_batch", sub: "same path as chat", desc: "Tool uses route through the same tool_router / executors the chat pipeline uses; approvals auto-resolve in agent context." },
    { id: "mail", group: "agents", col: 1, row: 1, label: "Mailbox", sub: "inter-agent msgs", desc: "Each agent has an in-memory mailbox on the MessageBus. send_message and broadcast enqueue here." },
    { id: "bus", group: "agents", col: 2, row: 1, kind: "store", label: "Event bus", sub: "per-session queue", desc: "A bounded (about 512) per-session asyncio queue. publish is thread-safe via call_soon_threadsafe." },
    { id: "sse", group: "transport", col: 3, row: 1, label: "SSE team_progress", sub: "drained per turn", desc: "The chat SSE consumer subscribes to the session channel and re-emits each event as a team_progress frame." },
    { id: "spa", group: "browser", col: 4, row: 1, label: "SPA report card", sub: "live agent log", desc: "The React SPA groups team_progress events by team_id and agent_id into a live report card." }
  ],
  edges: [
    { from: "chat", to: "runtime" },
    { from: "runtime", to: "tools" },
    { from: "runtime", to: "mail" },
    { from: "mail", to: "runtime", label: "poll each turn" },
    { from: "runtime", to: "bus", label: "publish" },
    { from: "bus", to: "sse" },
    { from: "sse", to: "spa" }
  ]
});
