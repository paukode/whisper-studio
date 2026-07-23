/* Tutorial - voice & meetings: from microphone to a chat summary */
WSDiagram.mount("voice-flow-diagram", {
  title: "Voice capture to summary",
  grid: { nodeW: 168, nodeH: 60, gapX: 54, gapY: 40 },
  groups: {
    browser: { label: "In the browser" },
    transport: { label: "Same origin" },
    local: { label: "On-device" }
  },
  nodes: [
    { id: "mic", group: "browser", col: 0, row: 0, label: "Microphone", sub: "getUserMedia", desc: "Your default input device. An AudioWorklet turns it into raw PCM samples in the page." },
    { id: "ws", group: "transport", col: 1, row: 0, label: "WebSocket /ws", sub: "PCM16", desc: "The page streams PCM16 audio chunks over a WebSocket to the local server. Nothing leaves your machine." },
    { id: "whisper", group: "local", col: 2, row: 0, label: "Whisper / Parakeet", sub: "on-device", desc: "The selected ASR engine runs locally and turns audio into text. Whisper transcribes whole utterances; Parakeet streams words as you speak." },
    { id: "diar", group: "local", col: 3, row: 0, label: "Speaker labels", sub: "diarization", desc: "Clustering assigns each segment a speaker (Speaker 1, Speaker 2). Later re-clustering can correct earlier labels." },
    { id: "panel", group: "browser", col: 4, row: 0, label: "Live transcript", sub: "right panel", desc: "Segments and a live draft row render in the Transcript panel, grouped by speaker, auto-scrolling to the newest line." },
    { id: "ask", group: "browser", col: 5, row: 0, label: "Summarize in chat", sub: "then /export", desc: "Ask Claude to summarize the discussion. Claude sees only the text transcript, never the audio. Export the result as Markdown." }
  ],
  edges: [
    { from: "mic", to: "ws", label: "PCM16" },
    { from: "ws", to: "whisper" },
    { from: "whisper", to: "diar" },
    { from: "diar", to: "panel", label: "segments" },
    { from: "panel", to: "ask" }
  ]
});
