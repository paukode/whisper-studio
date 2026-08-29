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
    { id: "whisper", group: "local", col: 2, row: 0, label: "ASR engine", sub: "on-device", desc: "The selected engine (Whisper, Parakeet, or Canary) runs locally and turns audio into text. Whisper transcribes whole utterances; Parakeet and Canary stream words as you speak. An optional translator (Canary or Apple) adds a translated line per segment." },
    { id: "diar", group: "local", col: 3, row: 0, label: "Speaker labels", sub: "diarization", desc: "Clustering assigns each segment a speaker (Speaker 1, Speaker 2). Later re-clustering can correct earlier labels." },
    { id: "panel", group: "browser", col: 4, row: 0, label: "Live transcript", sub: "right panel", desc: "Segments and a live draft row render in the Transcript panel, grouped by speaker. It follows the newest line while you are at the bottom; scroll up and a floating arrow jumps back to the latest." },
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
