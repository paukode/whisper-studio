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
    { id: "chat", group: "browser", col: 0, row: 0, label: "You chat", sub: "a turn ends", desc: "You send messages and Claude answers. You do not do a manual step for memory." },
    { id: "extract", group: "server", col: 1, row: 0, label: "Post-turn review", sub: "every third turn", desc: "A review in the background sends the last turn again and asks which facts to keep. It saves permanent facts as memories and procedures as skills. A separate agent updates the session summary. Neither stops your chat. In Local mode, the review does not run, and the app only reads saved memories." },
    { id: "files", group: "persist", kind: "store", col: 2, row: 0, label: "Memory files", sub: "data/memory/ + data/global_memory/", desc: "Permanent Markdown memory files in two tiers (project and global): user preferences, feedback, project goals, and references. Also a summary for each session in data/session_memory/. The data folder is ~/.whisper/data for the Mac app, and data/ in the checkout for a source install." },
    { id: "whmd", group: "persist", kind: "store", col: 2, row: 1, label: "WHISPER.md", sub: "rules/<project>/", desc: "The project instructions in rules/&lt;project&gt;/WHISPER.md in the user folder (~/.whisper for the Mac app), not in the workspace. Open it from More, Project memory. The app adds it to each chat turn, as CLAUDE.md for the Claude Code CLI." },
    { id: "inject", group: "server", col: 3, row: 0.5, label: "Injected into prompt", sub: "next turns", desc: "On the next turns, the app adds the related memories, the session summary, and WHISPER.md to the system prompt. Thus, Claude continues from the last point." }
  ],
  edges: [
    { from: "chat", to: "extract" },
    { from: "extract", to: "files", label: "write" },
    { from: "files", to: "inject", label: "recall" },
    { from: "whmd", to: "inject", label: "always" },
    { from: "inject", to: "chat", label: "next turn" }
  ]
});
