/* Tutorial - voice mode: who speaks, who works, where the answer lands */
WSDiagram.mount("voice-mode-diagram", {
  title: "A spoken turn, end to end",
  grid: { nodeW: 166, nodeH: 60, gapX: 52, gapY: 42 },
  groups: {
    browser: { label: "In the app" },
    cloud: { label: "Amazon Bedrock" },
    work: { label: "Your session" }
  },
  nodes: [
    { id: "you", group: "browser", col: 0, row: 0.5, label: "You speak", sub: "Talk button", desc: "The composer turns into the voice bar and your microphone streams as raw PCM16 at 16 kHz. Echo cancellation is on, because the answer is playing out of the same speakers." },
    { id: "ws", group: "browser", col: 1, row: 0.5, label: "WebSocket /ws/voice", sub: "audio up, audio down", desc: "One socket to the local server carries microphone frames up and the assistant's speech (24 kHz PCM16) plus every UI event down." },
    { id: "sonic", group: "cloud", kind: "external", col: 2, row: 0.5, label: "Nova 2 Sonic", sub: "the voice", desc: "Amazon Nova 2 Sonic hears you and speaks back over one bidirectional Bedrock stream. It handles the conversation, not the work: greetings, confirmations, and short spoken summaries." },
    { id: "ask", group: "work", col: 3, row: 0.5, label: "ask_assistant", sub: "hands it over", desc: "Anything concrete goes straight to the assistant as one written request that keeps your exact words for names, paths and commands." },
    { id: "claude", group: "work", col: 4, row: 0, label: "Your chat model", sub: "same tools", desc: "The model selected in the toolbar runs a normal turn with the full tool set, the connected workspace, your permission mode and this session's approvals. An on-device model falls back to the default cloud model." },
    { id: "card", group: "browser", col: 4, row: 1, label: "Request card", sub: "say it or click", desc: "An approval, a question or a folder choice pauses that turn. Sonic asks you out loud and the usual card appears; answering either way resumes the very same turn." },
    { id: "chat", group: "browser", col: 5, row: 0.5, label: "Chat transcript", sub: "spoken + written", desc: "One bubble per spoken utterance, marked spoken, plus the assistant's full written answer with its tool activity as its own bubble." }
  ],
  edges: [
    { from: "you", to: "ws", label: "PCM16" },
    { from: "ws", to: "sonic", label: "stream" },
    { from: "sonic", to: "ask", label: "tool call" },
    { from: "ask", to: "claude", label: "one turn" },
    { from: "claude", to: "card", label: "needs you" },
    { from: "card", to: "claude", label: "your answer" },
    { from: "claude", to: "chat", label: "written" },
    { from: "sonic", to: "chat", label: "spoken" }
  ]
});
