/* Tutorial - the direct save flow: destination resolved, content staged, one approval, one move */
WSDiagram.mount("save-flow-diagram", {
  title: "From request to clickable file, with one approval",
  grid: { nodeW: 168, nodeH: 60, gapX: 54, gapY: 44 },
  groups: {
    browser: { label: "In the chat" }, server: { label: "Local, on your machine" }
  },
  nodes: [
    { id: "ask", group: "browser", col: 0, row: 0, label: "You ask for a file", sub: "plain words", desc: "You ask for a report, deck, spreadsheet, PDF, or plain file. Naming a place ('in Downloads') is optional; you are never asked where." },
    { id: "resolve", group: "server", col: 1, row: 0, label: "Destination resolved", sub: "words > workspace > Documents", desc: "Your own words win (e.g. 'in Downloads' becomes ~/Downloads/report.docx). Otherwise a connected workspace resolves the path workspace-relative. With neither, the default is ~/Documents/<filename>." },
    { id: "build", group: "server", col: 2, row: 0, label: "Content built", sub: "finished bytes, tool time", desc: "The full document or file content is built before you see anything: docx/pptx/xlsx/pdf bytes in the standard layout, or the exact text/binary content for a plain file." },
    { id: "stage", group: "server", col: 3, row: 0, kind: "store", label: "Staged in sandbox", sub: "app staging area", desc: "The finished bytes are written to a staging area inside the app's own data folder. Nothing exists at the destination yet, and a declined save leaves nothing outside the sandbox." },
    { id: "card", group: "browser", col: 3, row: 1, label: "Approval card", sub: "shows the exact path", desc: "One card, inline in the chat, with the full destination path and size. This is the only human step; the file is already built, so approving cannot fail on content problems." },
    { id: "move", group: "server", col: 2, row: 1, label: "Moved into place", sub: "approve = one move", desc: "Approving moves the ready file from staging to the destination in a single filesystem move. The folder is remembered so the file stays openable and revealable later." },
    { id: "link", group: "browser", col: 1, row: 1, label: "Clickable link", sub: "opens in default app", desc: "The reply carries a link to the new file. Click it to open the file with its OS default app, the way a Finder double-click would; Cmd-click reveals it in Finder." }
  ],
  edges: [
    { from: "ask", to: "resolve" },
    { from: "resolve", to: "build" },
    { from: "build", to: "stage" },
    { from: "stage", to: "card" },
    { from: "card", to: "move", label: "Yes" },
    { from: "move", to: "link" }
  ]
});
