/* Tutorial: how a tool call flows through the permission mode into allow / ask / deny */
WSDiagram.mount("perm-flow-diagram", {
  title: "Permission flow: mode fans a tool call into allow, ask, or deny",
  grid: { nodeW: 168, nodeH: 60, gapX: 60, gapY: 40 },
  groups: {
    server: { label: "Chat pipeline" },
    security: { label: "Permissions" },
    tools: { label: "Tools" }
  },
  nodes: [
    { id: "call", group: "server", col: 0, row: 1, label: "Tool call", sub: "model asks for a tool", desc: "The model asks for a tool during the chat loop. Before the tool runs, the app examines the call against your mode." },
    { id: "mode", group: "security", col: 1, row: 1, label: "Permission mode", sub: "default … bypass", desc: "The Permissions control in the composer. It gives the call the result allow, ask, or deny." },
    { id: "allow", group: "tools", col: 2, row: 0, label: "Allow", sub: "run now", desc: "Reads come here, except reads of credential stores. In less strict modes, some writes also come here. The tool runs without a question." },
    { id: "ask", group: "security", col: 2, row: 1, label: "Ask", sub: "approval card", desc: "The turn stops and shows an approval card with the exact diff or command. You click Yes, Yes all, No, or Block." },
    { id: "deny", group: "security", col: 2, row: 2, label: "Deny", sub: "blocked", desc: "The app refuses the call. Plan mode blocks writes before dispatch. dontAsk refuses writes without a card. A session Block or a hard guard can also refuse a call." },
    { id: "run", group: "tools", col: 3, row: 0.5, label: "Run (sandboxed)", sub: "shell + code in sandbox-exec", desc: "The tool runs. Each shell command and each code run is in the OS sandbox, in all modes." }
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
