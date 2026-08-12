/* Architecture - approval & permissions (the consent boundary) */
WSDiagram.mount("approval-diagram", {
  title: "Permission evaluation and the pause-and-execute approval flow",
  grid: { nodeW: 162, nodeH: 60, gapX: 46, gapY: 42 },
  groups: {
    server: { label: "Chat pipeline" }, security: { label: "Permission logic" },
    tools: { label: "Executors" }, transport: { label: "SSE" }, browser: { label: "Client" }
  },
  nodes: [
    { id: "tool", group: "server", col: 0, row: 1, label: "Tool needs to run", sub: "from the tool loop", desc: "The model asked for a tool. Before dispatch, execute_tool_batch resolves a decision for it." },
    { id: "eval", group: "security", col: 1, row: 1, label: "Evaluate mode + rules", sub: "permissions.py", desc: "resolve_static_decision walks github-destructive, the MCP approve tier, category_modes, bypass, trusted skills, session approvals, custom rules, then the mode. Returns allow / ask / deny, or None to defer to the auto classifier." },
    { id: "allow", group: "tools", col: 2, row: 0, label: "Auto-allow", sub: "reads", desc: "Read-only tools and anything the mode or rules resolved to allow run immediately, no card." },
    { id: "pause", group: "security", col: 2, row: 1, label: "Pause the turn", sub: "paused_sessions", desc: "An ask decision stashes the in-flight turn state in paused_sessions (server/chat/engine/pause.py) and ends the stream instead of running the tool." },
    { id: "deny", group: "security", col: 2, row: 2, label: "Deny", sub: "blocked", desc: "A deny decision returns a denied tool_result to the model without running anything." },
    { id: "card", group: "transport", col: 3, row: 1, label: "SSE approval_request", sub: "approval card", desc: "An approval_request event carries the action, category, summary, preview, payload, risk hint, and risk explanation to the SPA, which renders an approval card." },
    { id: "user", group: "browser", col: 4, row: 1, label: "User approves", sub: "clicks Yes", desc: "The person reads the diff or command and decides. Nothing executes while the turn sits paused on the server." },
    { id: "exec", group: "server", col: 4, row: 2, label: "POST /approval/execute", sub: "{action, payload}", desc: "The client posts {action, payload}. The server looks the action up in the approval registry and dispatches that registered spec's executor - only registered ApprovalSpecs can ever run." },
    { id: "spec", group: "tools", col: 3, row: 2, label: "Approval spec runs", sub: "executor", desc: "registry.get(action).executor(payload) performs the write, delete, command, save, or git action and returns an outcome. The turn resumes via a new /api/chat turn carrying approved_tool_result." }
  ],
  edges: [
    { from: "tool", to: "eval" },
    { from: "eval", to: "allow", label: "allow" },
    { from: "eval", to: "pause", label: "ask" },
    { from: "eval", to: "deny", label: "deny" },
    { from: "pause", to: "card" },
    { from: "card", to: "user" },
    { from: "user", to: "exec" },
    { from: "exec", to: "spec" }
  ]
});
