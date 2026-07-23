/* Tutorial - dropping a document into chat and asking about it */
WSDiagram.mount("doc-flow-diagram", {
  title: "Document to answer, with follow-ups",
  grid: { nodeW: 168, nodeH: 60, gapX: 52, gapY: 40 },
  groups: {
    browser: { label: "In the app" }, server: { label: "Local, on your machine" },
    external: { label: "Cloud" }
  },
  nodes: [
    { id: "drop", group: "browser", col: 0, row: 0, label: "Drop file in chat", sub: "or Attach", desc: "Drag a PDF, Word doc, deck, spreadsheet, or image onto the composer, or use the paperclip button." },
    { id: "extract", group: "server", col: 1, row: 0, label: "Extract locally", sub: "to Markdown / OCR", desc: "The file is converted to text on your laptop. Scanned PDFs and images are OCR'd; the bytes never leave the machine yet." },
    { id: "chip", group: "browser", col: 2, row: 0, label: "File chip", sub: "shows name", desc: "A chip with the file name appears in the composer once the upload resolves. Click the x to remove it." },
    { id: "ask", group: "browser", col: 3, row: 0, label: "Ask a question", sub: "plain English", desc: "Type your question and press Enter. The extracted document text is sent alongside it." },
    { id: "claude", group: "external", kind: "external", col: 4, row: 0, label: "Claude", sub: "text + doc", desc: "Claude reads your question and the document text once, then streams an answer back." },
    { id: "answer", group: "browser", col: 5, row: 0, label: "Answer + follow-ups", sub: "doc stays in context", desc: "The answer streams into the chat. The document stays in the conversation, so follow-ups need no re-upload." }
  ],
  edges: [
    { from: "drop", to: "extract" },
    { from: "extract", to: "chip" },
    { from: "chip", to: "ask" },
    { from: "ask", to: "claude" },
    { from: "claude", to: "answer" },
    { from: "answer", to: "ask", label: "follow-up" }
  ]
});
