/* Tutorial: a session goes out as portable JSONL and comes back the same way, or forks in place */
WSDiagram.mount("sessions-flow-diagram", {
  title: "Export, import, and fork, side by side",
  grid: { nodeW: 172, nodeH: 60, gapX: 56, gapY: 44 },
  groups: {
    browser: { label: "Sidebar" }, server: { label: "Backend" }, file: { label: "Portable file" }
  },
  nodes: [
    { id: "session", group: "browser", col: 0, row: 0.5, label: "A session", sub: "conversation + transcript", desc: "A conversation in the sidebar, with its chat history and its recorded transcript, if there is one." },
    { id: "export", group: "server", col: 1, row: 0, label: "Export", sub: "one, or select many", desc: "GET .../export sends one session as JSONL. In select mode, Export puts the JSONL of each selected session into one zip file." },
    { id: "jsonl", group: "file", kind: "store", col: 2, row: 0, label: ".jsonl (or .zip of them)", sub: "one export_meta line, then segments and messages", desc: "A plain-text file with one JSON object on each line. You can attach it to a bug report or move it to a different Mac." },
    { id: "import", group: "server", col: 3, row: 0, label: "Import", sub: "one file, or several / a .zip", desc: "POST .../import-transcript (one file) or .../bulk-import (many files, or a bulk-export .zip) makes one new session for each file. The app identifies a zip file from its content." },
    { id: "newsession", group: "browser", col: 4, row: 0.5, label: "New session(s)", sub: "same content, new id", desc: "The sidebar opens the new session, or the last session of a batch. Each new session has a new ID and its own history." },
    { id: "branch", group: "server", col: 2, row: 1, label: "Branch", sub: "in-app copy, no file", desc: "POST .../branch copies the saved content of the session into a new row in the same database. Branch does not make a file." }
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
