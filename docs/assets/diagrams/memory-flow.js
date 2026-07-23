/* Tutorial - how memory flows across chat turns */
WSDiagram.mount("memory-flow-diagram", {
  title: "Memory across sessions",
  grid: { nodeW: 168, nodeH: 62, gapX: 56, gapY: 46 },
  groups: {
    browser: { label: "You" },
    server: { label: "Chat pipeline" },
    persist: { label: "On disk" }
  },
  nodes: [
    { id: "chat", group: "browser", col: 0, row: 0, label: "You chat", sub: "a turn ends", desc: "You send messages and Claude answers. Nothing about memory is manual here." },
    { id: "extract", group: "server", col: 1, row: 0, label: "Background extract", sub: "after N turns", desc: "Once the conversation crosses a size (and, in the cloud, tool-use) threshold, a background agent distils facts and a session summary. It never blocks your chat." },
    { id: "files", group: "persist", kind: "store", col: 2, row: 0, label: "Memory files", sub: "data/memory/ + data/global_memory/", desc: "Durable Markdown memory files across two tiers (project + global): user preferences, feedback, project goals, and reference pointers, plus a per-session summary." },
    { id: "whmd", group: "persist", kind: "store", col: 2, row: 1, label: "WHISPER.md", sub: "project context", desc: "A file you write in your workspace root. Loaded on every chat turn, like CLAUDE.md for the Claude Code CLI." },
    { id: "inject", group: "server", col: 3, row: 0.5, label: "Injected into prompt", sub: "next turns", desc: "On the next turns, relevant memories, the session summary, and WHISPER.md are folded into the system prompt so Claude picks up where you left off." }
  ],
  edges: [
    { from: "chat", to: "extract" },
    { from: "extract", to: "files", label: "write" },
    { from: "files", to: "inject", label: "recall" },
    { from: "whmd", to: "inject", label: "always" },
    { from: "inject", to: "chat", label: "next turn" }
  ]
});
