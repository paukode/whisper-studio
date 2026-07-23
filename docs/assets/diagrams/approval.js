/* Architecture - approval & permissions (the consent boundary) */
WSDiagram.mount("approval-diagram", {
  title: "Permission evaluation and the nonce approval flow",
  grid: { nodeW: 162, nodeH: 60, gapX: 46, gapY: 42 },
  groups: {
    server: { label: "Chat pipeline" }, security: { label: "Permission logic" },
    tools: { label: "Executors" }, transport: { label: "SSE" }, browser: { label: "Client" }
  },
  nodes: [
    { id: "tool", group: "server", col: 0, row: 1, label: "Tool needs to run", sub: "from the tool loop", desc: "The model asked for a tool. Before dispatch, execute_tool_batch resolves a decision for it." },
    { id: "eval", group: "security", col: 1, row: 1, label: "Evaluate mode + rules", sub: "permissions.py", desc: "resolve_static_decision walks bypass, trusted skills, session approvals, custom rules, then the mode. Returns allow / ask / deny, or None to defer to the auto classifier." },
    { id: "allow", group: "tools", col: 2, row: 0, label: "Auto-allow", sub: "reads", desc: "Read-only tools and anything the mode or rules resolved to allow run immediately, no card." },
    { id: "nonce", group: "security", col: 2, row: 1, label: "Issue nonce", sub: "pause turn", desc: "An ask decision mints a one-time nonce and stashes the turn state in _paused_sessions." },
    { id: "deny", group: "security", col: 2, row: 2, label: "Deny", sub: "blocked", desc: "A deny decision returns a denied tool_result to the model without running anything." },
    { id: "card", group: "transport", col: 3, row: 1, label: "SSE user_question", sub: "approval card", desc: "A user_question event carries the summary, preview, and nonce to the SPA, which renders an approval card." },
    { id: "user", group: "browser", col: 4, row: 1, label: "User approves", sub: "clicks Yes", desc: "The person reads the diff or command and decides. Only a real click produces the matching nonce." },
    { id: "exec", group: "server", col: 4, row: 2, label: "POST /approval/execute", sub: "verify nonce", desc: "The client posts {action, payload, nonce}. The server verifies the nonce, then dispatches the registered approval spec." },
    { id: "spec", group: "tools", col: 3, row: 2, label: "Approval spec runs", sub: "executor", desc: "registry.get(action).executor(payload) performs the write, delete, command, or git action and returns an outcome. The turn resumes with approved_tool_result." }
  ],
  edges: [
    { from: "tool", to: "eval" },
    { from: "eval", to: "allow", label: "allow" },
    { from: "eval", to: "nonce", label: "ask" },
    { from: "eval", to: "deny", label: "deny" },
    { from: "nonce", to: "card" },
    { from: "card", to: "user" },
    { from: "user", to: "exec" },
    { from: "exec", to: "spec" }
  ]
});
