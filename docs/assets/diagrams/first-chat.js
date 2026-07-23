/* Tutorial - your first chat: one turn and its follow-up loop */
WSDiagram.mount("first-chat-diagram", {
  title: "A first chat turn, end to end",
  grid: { nodeW: 168, nodeH: 60, gapX: 54, gapY: 40 },
  groups: {
    browser: { label: "In the browser" },
    transport: { label: "Transport" },
    server: { label: "Server" },
    external: { label: "Bedrock" }
  },
  nodes: [
    { id: "type", group: "browser", col: 0, row: 0, label: "Type a message", sub: "the composer", desc: "Write in the input box. Enter sends; Shift+Enter adds a newline." },
    { id: "send", group: "browser", col: 1, row: 0, label: "Send (Enter)", sub: "or voice trigger", desc: "Press Enter, click Send, or finish a dictation with a phrase like 'send now'." },
    { id: "api", group: "transport", col: 2, row: 0, label: "POST /api/chat", sub: "SSE response", desc: "The turn is sent to the backend, which replies as a Server-Sent Events stream, not one JSON body." },
    { id: "claude", group: "external", kind: "external", col: 3, row: 0, label: "Claude", sub: "Amazon Bedrock", desc: "The selected model runs on Amazon Bedrock and streams its answer back token by token." },
    { id: "stream", group: "server", col: 4, row: 0, label: "Stream answer", sub: "token by token", desc: "Text arrives in fragments and is forwarded to the browser as it is produced." },
    { id: "read", group: "browser", col: 5, row: 0, label: "Read + follow up", sub: "keeps history", desc: "You read the streamed reply and can ask again. The whole conversation is saved and re-sent as context." }
  ],
  edges: [
    { from: "type", to: "send" },
    { from: "send", to: "api" },
    { from: "api", to: "claude" },
    { from: "claude", to: "stream" },
    { from: "stream", to: "read" },
    { from: "read", to: "api", label: "follow-up + history" }
  ]
});
