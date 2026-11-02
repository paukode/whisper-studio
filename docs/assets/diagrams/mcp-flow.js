/* Tutorial - extending Whisper Studio with MCP servers and plugins */
WSDiagram.mount("mcp-flow-diagram", {
  title: "How extensions reach chat",
  grid: { nodeW: 172, nodeH: 60, gapX: 56, gapY: 44 },
  groups: {
    browser: { label: "Settings UI" }, server: { label: "Backend" },
    external: { label: "External" }, tools: { label: "Chat" }
  },
  nodes: [
    { id: "cfg", group: "browser", col: 0, row: 0.5, label: "Configure in Settings", sub: "MCP / Plugins", desc: "Add an MCP server (a command, or a remote url) in Settings, MCP servers. Or put a .py file in the plugins folder (~/.whisper/plugins for the Mac app, plugins/ in the checkout for a source install) and open Settings, Plugins." },
    { id: "enable", group: "server", col: 1, row: 0.5, label: "Enable", sub: "MCP opt-out, plugins opt-in", desc: "When you add an MCP server, the app enables and starts it. Each server has an enabled flag in Settings, MCP servers. A plugin does nothing until you enable it in Settings. Most plugin changes apply immediately, with no restart." },
    { id: "mcp", group: "external", kind: "external", col: 2, row: 0, label: "MCP server", sub: "stdio or remote HTTP", desc: "An external MCP server: a stdio child process that the backend starts, or a remote server over streamable HTTP with an optional bearer token. By default (approval_mode auto), its tools run with no approval card." },
    { id: "plug", group: "server", col: 2, row: 1, label: "Plugin", sub: "plugins/*.py", desc: "A local Python file. Its register(app, executor_registry) function adds tool executors, API routes, or PreToolUse hooks in the backend process." },
    { id: "use", group: "tools", col: 3, row: 0.5, label: "Tools available in chat", sub: "the model can call them", desc: "Both methods add tools that the assistant can call during a turn, with the built-in tools." }
  ],
  edges: [
    { from: "cfg", to: "enable" },
    { from: "enable", to: "mcp", label: "persisted toggle" },
    { from: "enable", to: "plug", label: "live, mostly" },
    { from: "mcp", to: "use" },
    { from: "plug", to: "use" }
  ]
});
