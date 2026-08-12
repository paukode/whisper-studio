/* Tutorial - a session leaves as portable JSONL and comes back the same way, or forks in place */
WSDiagram.mount("sessions-flow-diagram", {
  title: "Export, import, and fork, side by side",
  grid: { nodeW: 172, nodeH: 60, gapX: 56, gapY: 44 },
  groups: {
    browser: { label: "Sidebar" }, server: { label: "Backend" }, file: { label: "Portable file" }
  },
  nodes: [
    { id: "session", group: "browser", col: 0, row: 0.5, label: "A session", sub: "conversation + transcript", desc: "Any conversation in the sidebar, with its chat history and any recorded transcript." },
    { id: "export", group: "server", col: 1, row: 0, label: "Export", sub: "one, or select many", desc: "GET .../export streams one session as JSONL. Select several and Export zips each one's JSONL into a single download." },
    { id: "jsonl", group: "file", kind: "store", col: 2, row: 0, label: ".jsonl (or .zip of them)", sub: "one export_meta line, then segments and messages", desc: "A plain-text, line-delimited file: portable, human-diffable, safe to attach to a bug report or move to another machine." },
    { id: "import", group: "server", col: 3, row: 0, label: "Import", sub: "one file, or several / a .zip", desc: "POST .../import-transcript (one file) or .../bulk-import (many files, or a bulk-export .zip, detected by content) inserts a brand-new session per file." },
    { id: "newsession", group: "browser", col: 4, row: 0.5, label: "New session(s)", sub: "same content, new id", desc: "The sidebar switches to the newly imported session (or the last of a batch), each with a fresh id and its own history from here." },
    { id: "branch", group: "server", col: 2, row: 1, label: "Branch", sub: "in-app copy, no file", desc: "POST .../branch copies the current session's persisted content straight into a new row in the same database. No file changes hands." }
  ],
  edges: [
    { from: "session", to: "export" },
    { from: "export", to: "jsonl" },
    { from: "jsonl", to: "import" },
    { from: "import", to: "newsession" },
    { from: "session", to: "branch", label: "Branch" },
    { from: "branch", to: "newsession" }
  ]
});
