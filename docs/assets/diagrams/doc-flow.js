/* Tutorial: dropping a document into chat and asking about it */
WSDiagram.mount("doc-flow-diagram", {
  title: "Document to answer, with follow-ups",
  grid: { nodeW: 168, nodeH: 60, gapX: 52, gapY: 40 },
  groups: {
    browser: { label: "In the app" }, server: { label: "Local, on your machine" },
    external: { label: "The model" }
  },
  nodes: [
    { id: "drop", group: "browser", col: 0, row: 0, label: "Drop file in chat", sub: "or Attach", desc: "Drag a PDF, a Word document, a deck, a spreadsheet, or an image on the composer, or click the paperclip button." },
    { id: "extract", group: "server", col: 1, row: 0, label: "Extract locally", sub: "to Markdown / OCR", desc: "The app converts the file to text on your Mac. The app uses OCR for scanned PDFs and images. The file does not go off the Mac." },
    { id: "chip", group: "browser", col: 2, row: 0, label: "File chip", sub: "shows name", desc: "A chip with the file name shows in the composer. When the upload completes, you can send. Click the x to remove the file." },
    { id: "ask", group: "browser", col: 3, row: 0, label: "Ask a question", sub: "plain English", desc: "Type your question and press Enter. The app sends the extracted text with the question." },
    { id: "model", group: "external", kind: "external", col: 4, row: 0, label: "Selected model", sub: "cloud or on-device", desc: "The model reads your question and the document text, then returns the answer as a stream. In Local mode, the model is on-device and the text stays on your Mac." },
    { id: "answer", group: "browser", col: 5, row: 0, label: "Answer + follow-ups", sub: "doc stays in context", desc: "The answer shows in the chat. The document stays in the conversation, so you do not upload it again for the next question." }
  ],
  edges: [
    { from: "drop", to: "extract" },
    { from: "extract", to: "chip" },
    { from: "chip", to: "ask" },
    { from: "ask", to: "model" },
    { from: "model", to: "answer" },
    { from: "answer", to: "ask", label: "follow-up" }
  ]
});
