/* Architecture - tool dispatch & executors (routing, batching, approval) */
WSDiagram.mount("tools-diagram", {
  title: "Tool dispatch and execution",
  grid: { nodeW: 160, nodeH: 60, gapX: 44, gapY: 40 },
  groups: {
    server: { label: "Model" }, tools: { label: "Executor layer" },
    security: { label: "Consent" }, external: { label: "MCP" }, transport: { label: "Transport" }
  },
  nodes: [
    { id: "model", group: "server", col: 0, row: 1, label: "Model tool_use", sub: "one or more calls", desc: "The model returns a batch of tool_use blocks in a single assistant message." },
    { id: "batch", group: "tools", col: 1, row: 1, label: "execute_tool_batch", sub: "tool_executor.py", desc: "Owns the lifecycle: partitioning, permission checks, hooks, approvals, and result shaping." },
    { id: "part", group: "tools", col: 2, row: 1, label: "Partition", sub: "reads parallel · writes serial", desc: "Consecutive concurrent-safe tools form one parallel batch; each unsafe tool runs on its own." },
    { id: "perm", group: "security", col: 3, row: 1, label: "Permission check", sub: "mode + rules", desc: "Each tool is resolved against the current mode and per-tool rules: allow, ask, or deny." },
    { id: "appr", group: "security", col: 3, row: 2, label: "Approval", sub: "nonce pause", desc: "A tool that needs consent emits an approval_request, pauses the turn, and waits for a client-confirmed nonce." },
    { id: "route", group: "tools", col: 4, row: 1, label: "route_tool", sub: "tool_router.py", desc: "Pure dispatch: maps a tool name to its handler. No state, no lifecycle." },
    { id: "reg", group: "tools", col: 4, row: 0, label: "Executor registry", sub: "executors/", desc: "@register_executor handlers with read_only / concurrent_safe / destructive metadata." },
    { id: "mcp", group: "external", kind: "external", col: 4, row: 2, label: "MCP tool", sub: "stdio", desc: "execute_mcp_tool forwards the call to a connected MCP server over stdio." },
    { id: "result", group: "transport", col: 5, row: 1, label: "Result + SSE", sub: "tool_result · events", desc: "Output becomes a tool_result for the next round; side effects stream to the SPA as SSE events." }
  ],
  edges: [
    { from: "model", to: "batch" },
    { from: "batch", to: "part" },
    { from: "part", to: "perm" },
    { from: "perm", to: "appr", label: "write" },
    { from: "appr", to: "route", label: "approved" },
    { from: "perm", to: "route", label: "allow" },
    { from: "route", to: "reg" },
    { from: "route", to: "mcp" },
    { from: "reg", to: "result" },
    { from: "mcp", to: "result" }
  ]
});
