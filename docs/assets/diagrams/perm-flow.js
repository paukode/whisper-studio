/* Tutorial - how a tool call flows through the permission mode into allow / ask / deny */
WSDiagram.mount("perm-flow-diagram", {
  title: "Permission flow: mode fans a tool call into allow, ask, or deny",
  grid: { nodeW: 168, nodeH: 60, gapX: 60, gapY: 40 },
  groups: {
    server: { label: "Chat pipeline" },
    security: { label: "Permissions" },
    tools: { label: "Tools" }
  },
  nodes: [
    { id: "call", group: "server", col: 0, row: 1, label: "Tool call", sub: "model asks for a tool", desc: "The model requests a tool during the streaming loop. Before anything runs, the call is checked against your current mode and per-tool rules." },
    { id: "mode", group: "security", col: 1, row: 1, label: "Permission mode", sub: "default … bypass", desc: "The Mode dial in the chat input. Combined with any per-tool rules in Settings, it resolves the call to allow, ask, or deny." },
    { id: "allow", group: "tools", col: 2, row: 0, label: "Allow", sub: "run now", desc: "Reads always land here. In looser modes some writes do too. The tool runs with no prompt." },
    { id: "ask", group: "security", col: 2, row: 1, label: "Ask", sub: "approval card", desc: "The turn pauses and shows an approval card with the exact diff or command. You click Approve or Deny." },
    { id: "deny", group: "security", col: 2, row: 2, label: "Deny", sub: "blocked", desc: "The call is refused. Plan mode blocks writes pre-dispatch; dontAsk denies writes silently; a deny rule or a hard guard can also land here." },
    { id: "run", group: "tools", col: 3, row: 0.5, label: "Run (sandboxed)", sub: "shell + code in sandbox-exec", desc: "The tool's implementation executes. Every shell and code run is wrapped in the OS sandbox regardless of how it was approved." }
  ],
  edges: [
    { from: "call", to: "mode" },
    { from: "mode", to: "allow" },
    { from: "mode", to: "ask" },
    { from: "mode", to: "deny" },
    { from: "allow", to: "run" },
    { from: "ask", to: "run", label: "approve" }
  ]
});
