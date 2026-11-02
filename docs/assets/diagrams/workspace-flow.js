/* Tutorial: the workspace IDE loop: connect, describe, tools, approve, result */
WSDiagram.mount("workspace-flow-diagram", {
  title: "The workspace loop",
  grid: { nodeW: 168, nodeH: 62, gapX: 54, gapY: 44 },
  groups: {
    browser: { label: "You (the IDE)" },
    tools: { label: "The assistant" },
    security: { label: "Approval" }
  },
  nodes: [
    { id: "connect", group: "browser", col: 0, row: 0.5, label: "Connect a folder", sub: "Workspace control", desc: "Select a project folder. When the folder is connected, the assistant can use the workspace tools." },
    { id: "tree", group: "browser", col: 1, row: 0.5, label: "File tree + editor", sub: "Monaco", desc: "Open files from the tree in Monaco editor tabs. You can also type commands in the xterm terminal." },
    { id: "ask", group: "browser", col: 2, row: 0.5, label: "Describe a task", sub: "chat + @file:path", desc: "Write the task in plain English. Refer to files with @file:path, and the app adds their content to the prompt." },
    { id: "tools", group: "tools", col: 3, row: 0.5, label: "The assistant uses ws_* tools", sub: "read · edit · run", desc: "The assistant reads, finds, edits, and makes files, and runs commands, with the ws_* and git_* tools." },
    { id: "approve", group: "security", col: 4, row: 0, label: "Approval card", sub: "for writes", desc: "When the permission mode asks, a tool that changes files, runs a command, or changes git shows a card first. The card shows the exact diff or command." },
    { id: "result", group: "browser", col: 5, row: 0.5, label: "Diff + git status", sub: "review the change", desc: "The editor shows the diff, and the Git Changes panel shows the change. The tree refreshes automatically." }
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
