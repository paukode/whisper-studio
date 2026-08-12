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
    { id: "route", group: "tools", col: 3, row: 1, label: "route_tool", sub: "tool_router.py", desc: "Pure dispatch: maps a tool name to its handler and runs it once, unconditionally. A gated action's own executor returns the [WS_APPROVAL] sentinel here rather than acting yet." },
    { id: "reg", group: "tools", col: 4, row: 0, label: "Executor registry", sub: "executors/", desc: "@register_executor handlers with read_only / concurrent_safe / destructive metadata." },
    { id: "mcp", group: "external", kind: "external", col: 4, row: 2, label: "MCP tool", sub: "stdio / HTTP", desc: "execute_mcp_tool forwards the call to a connected MCP server, over stdio for a local process or streamable HTTP for a remote server." },
    { id: "perm", group: "security", col: 5, row: 0, label: "Permission check", sub: "mode + rules", desc: "In process_tool_results, any [WS_APPROVAL] output is resolved against the current mode and per-tool rules: allow, ask, or deny." },
    { id: "appr", group: "security", col: 5, row: 2, label: "Approval", sub: "pause + resume", desc: "A tool that needs consent pauses the turn in the server-held pause store; the client's decision resumes it." },
    { id: "specexec", group: "security", col: 6, row: 1, label: "ApprovalSpec executor", sub: "approval/registry.py", desc: "The action's own executor, re-validating everything at execute time - reached on an immediate allow or after the user approves, never by calling route_tool again." },
    { id: "result", group: "transport", col: 7, row: 1, label: "Result + SSE", sub: "tool_result · events", desc: "Output becomes a tool_result for the next round; side effects stream to the SPA as SSE events." }
  ],
  edges: [
    { from: "model", to: "batch" },
    { from: "batch", to: "part" },
    { from: "part", to: "route" },
    { from: "route", to: "reg" },
    { from: "route", to: "mcp" },
    { from: "reg", to: "result" },
    { from: "mcp", to: "result" },
    { from: "reg", to: "perm", label: "[WS_APPROVAL]" },
    { from: "perm", to: "appr", label: "ask" },
    { from: "perm", to: "specexec", label: "allow" },
    { from: "appr", to: "specexec", label: "approved" },
    { from: "specexec", to: "result" }
  ]
});
