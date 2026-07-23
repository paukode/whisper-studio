/* Tutorial - the workspace IDE loop: connect, describe, tools, approve, result */
WSDiagram.mount("workspace-flow-diagram", {
  title: "The workspace loop",
  grid: { nodeW: 168, nodeH: 62, gapX: 54, gapY: 44 },
  groups: {
    browser: { label: "You (the IDE)" },
    tools: { label: "Claude" },
    security: { label: "Approval" }
  },
  nodes: [
    { id: "connect", group: "browser", col: 0, row: 0.5, label: "Connect a folder", sub: "Workspace control", desc: "Point Whisper Studio at a project directory. Once connected, the workspace tools switch on." },
    { id: "tree", group: "browser", col: 1, row: 0.5, label: "File tree + editor", sub: "Monaco", desc: "Browse the file tree, open tabs in the Monaco editor, and run the xterm terminal yourself." },
    { id: "ask", group: "browser", col: 2, row: 0.5, label: "Describe a task", sub: "chat + @file:path", desc: "Ask in plain English. Reference files inline with @file:path so Claude sees their contents." },
    { id: "tools", group: "tools", col: 3, row: 0.5, label: "Claude uses ws_* tools", sub: "read · edit · run", desc: "Claude reads, greps, globs, edits, creates, and runs commands through the ws_* and git_* tools." },
    { id: "approve", group: "security", col: 4, row: 0, label: "Approval card", sub: "for writes", desc: "Any tool that changes files, runs a command, or alters git shows a card with the exact diff or command before it happens." },
    { id: "result", group: "browser", col: 5, row: 0.5, label: "Diff + git status", sub: "review the change", desc: "See the applied diff in the editor and the change in the Git Changes panel; the tree refreshes automatically." }
  ],
  edges: [
    { from: "connect", to: "tree" },
    { from: "tree", to: "ask" },
    { from: "ask", to: "tools" },
    { from: "tools", to: "approve", label: "write" },
    { from: "approve", to: "result" },
    { from: "tools", to: "result", label: "read" }
  ]
});
