/* Tutorial: your first chat: one turn and its follow-up loop */
WSDiagram.mount("first-chat-diagram", {
  title: "A first chat turn, end to end",
  grid: { nodeW: 168, nodeH: 60, gapX: 54, gapY: 40 },
  groups: {
    browser: { label: "In the browser" },
    transport: { label: "Transport" },
    server: { label: "Server" },
    external: { label: "The model" }
  },
  nodes: [
    { id: "type", group: "browser", col: 0, row: 0, label: "Type a message", sub: "the composer", desc: "Type in the input box. Enter sends the message. Shift+Enter adds a new line." },
    { id: "send", group: "browser", col: 1, row: 0, label: "Send (Enter)", sub: "or voice trigger", desc: "Press Enter, click Send, or say a phrase such as 'send now' at the end of a dictation." },
    { id: "api", group: "transport", col: 2, row: 0, label: "POST /api/chat", sub: "SSE response", desc: "The composer sends the turn to the backend. The backend returns a Server-Sent Events stream, not one JSON body." },
    { id: "model", group: "external", kind: "external", col: 3, row: 0, label: "Selected model", sub: "Bedrock or on-device", desc: "The model that you selected: Claude or GPT on Amazon Bedrock, or an on-device model. The model mode controls which models you can select. The model returns its answer in small parts." },
    { id: "stream", group: "server", col: 4, row: 0, label: "Stream answer", sub: "token by token", desc: "The server sends each part of the text to the browser immediately." },
    { id: "read", group: "browser", col: 5, row: 0, label: "Read + follow up", sub: "keeps history", desc: "You read the answer and ask again. The app saves the full conversation and sends it again as context." }
  ],
  edges: [
    { from: "type", to: "send" },
    { from: "send", to: "api" },
    { from: "api", to: "model" },
    { from: "model", to: "stream" },
    { from: "stream", to: "read" },
    { from: "read", to: "api", label: "follow-up + history" }
  ]
});
