/* Tutorial - extending Whisper Studio with MCP servers and plugins */
WSDiagram.mount("mcp-flow-diagram", {
  title: "How extensions reach chat",
  grid: { nodeW: 172, nodeH: 60, gapX: 56, gapY: 44 },
  groups: {
    browser: { label: "Settings UI" }, server: { label: "Backend" },
    external: { label: "External" }, tools: { label: "Chat" }
  },
  nodes: [
    { id: "cfg", group: "browser", col: 0, row: 0.5, label: "Configure in Settings", sub: "MCP / Plugins", desc: "Add an MCP server command or drop a .py file into plugins/, then open the matching Settings tab." },
    { id: "enable", group: "server", col: 1, row: 0.5, label: "Enable", sub: "opt-in", desc: "Nothing loads until you turn it on. MCP servers toggle per turn from the chat toolbar; plugins enable in Settings and load on restart." },
    { id: "mcp", group: "external", kind: "external", col: 2, row: 0, label: "MCP server", sub: "stdio child process", desc: "An external MCP server the backend launches as a child process and talks to over stdio (Model Context Protocol)." },
    { id: "plug", group: "server", col: 2, row: 1, label: "Plugin", sub: "plugins/*.py", desc: "A local Python file whose register(app, executor_registry) adds tool executors, API routes, or PreToolUse hooks in-process." },
    { id: "use", group: "tools", col: 3, row: 0.5, label: "Tools available in chat", sub: "the model can call them", desc: "Both paths surface new tools the assistant can call during a turn, alongside the built-in tool set." }
  ],
  edges: [
    { from: "cfg", to: "enable" },
    { from: "enable", to: "mcp", label: "per turn" },
    { from: "enable", to: "plug", label: "on restart" },
    { from: "mcp", to: "use" },
    { from: "plug", to: "use" }
  ]
});
