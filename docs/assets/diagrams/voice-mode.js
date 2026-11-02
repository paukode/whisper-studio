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
    { id: "you", group: "browser", col: 0, row: 0.5, label: "You speak", sub: "Talk button", desc: "The voice bar replaces the chat box, and the app sends your microphone audio as PCM16 at 16 kHz. Echo cancellation is on, because the answer comes from the same speakers. Talk works only in Hybrid and Cloud mode." },
    { id: "ws", group: "browser", col: 1, row: 0.5, label: "WebSocket /ws/voice", sub: "audio up, audio down", desc: "One socket to the local server sends the microphone audio. It receives the speech of the assistant (24 kHz PCM16) and the UI events." },
    { id: "sonic", group: "cloud", kind: "external", col: 2, row: 0.5, label: "Nova 2 Sonic", sub: "the voice", desc: "Amazon Nova 2 Sonic receives your speech and answers with speech over one bidirectional Bedrock stream. It does the conversation, not the work: greetings, questions to you, and short spoken summaries." },
    { id: "ask", group: "work", col: 3, row: 0.5, label: "ask_assistant", sub: "hands it over", desc: "Nova 2 Sonic sends each work request to the assistant as one written request. The request keeps your exact words for names, paths, and commands." },
    { id: "claude", group: "work", col: 4, row: 0, label: "Your chat model", sub: "same tools", desc: "The model in the toolbar runs a usual turn with all the tools, the connected workspace, your permission mode, and the approvals of this session. If the selected model is on-device, the app refuses the request and tells you to select a cloud model." },
    { id: "card", group: "browser", col: 4, row: 1, label: "Request card", sub: "say it or click", desc: "An approval, a question, or a folder choice stops that turn. Nova 2 Sonic asks you aloud, and the usual card shows in the chat. An answer with your voice or with a click continues the same turn." },
    { id: "chat", group: "browser", col: 5, row: 0.5, label: "Chat transcript", sub: "spoken + written", desc: "Your speech shows as one bubble for each turn. The words and the text of the assistant in a turn show in one bubble: the spoken parts, and the full written answer with its tool activity. The bubble shows the mark spoken when a part of it was spoken." }
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
