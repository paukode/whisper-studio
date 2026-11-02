/* Tutorial: the direct save flow: destination resolved, content staged, one approval, one move */
WSDiagram.mount("save-flow-diagram", {
  title: "From request to clickable file, with one approval",
  grid: { nodeW: 168, nodeH: 60, gapX: 54, gapY: 44 },
  groups: {
    browser: { label: "In the chat" }, server: { label: "Local, on your machine" }
  },
  nodes: [
    { id: "ask", group: "browser", col: 0, row: 0, label: "You ask for a file", sub: "plain words", desc: "You ask for a report, a deck, a spreadsheet, a PDF, or a plain file. You can name a location ('in Downloads'). The app does not ask where." },
    { id: "resolve", group: "server", col: 1, row: 0, label: "Destination resolved", sub: "words > workspace > Documents", desc: "Your words have priority ('in Downloads' gives ~/Downloads/report.docx). If you name no location, a connected workspace gives a path in the project. If there is no workspace, the default is ~/Documents/<filename>." },
    { id: "build", group: "server", col: 2, row: 0, label: "Content built", sub: "finished bytes, tool time", desc: "The tool makes the full file before the card shows: a docx, pptx, xlsx, or pdf file in the standard layout, or the exact content of a plain file." },
    { id: "stage", group: "server", col: 3, row: 0, kind: "store", label: "Staged in sandbox", sub: "app staging area", desc: "The tool writes the finished file to a staging area in the data folder of the app. Nothing is at the destination yet. A refused save leaves nothing at the destination." },
    { id: "card", group: "browser", col: 3, row: 1, label: "Approval card", sub: "shows the exact path", desc: "One card in the chat shows the full destination path and the size. This is the only step for you. The file is complete, so the content cannot fail after approval." },
    { id: "move", group: "server", col: 2, row: 1, label: "Moved into place", sub: "approve = one move", desc: "The approval moves the finished file from the staging area to the destination in one move. The app keeps the folder, so you can open the file later." },
    { id: "link", group: "browser", col: 1, row: 1, label: "Clickable link", sub: "opens in default app", desc: "The answer contains a link to the new file. Click the link to open the file in its default app. Cmd-click the link to show the file in Finder." }
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
