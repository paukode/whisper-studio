/* Tutorial - voice & meetings: from microphone to a chat summary */
WSDiagram.mount("voice-flow-diagram", {
  title: "Voice capture to summary",
  grid: { nodeW: 168, nodeH: 60, gapX: 54, gapY: 40 },
  groups: {
    browser: { label: "In the app" },
    transport: { label: "Same origin" },
    local: { label: "On this Mac" }
  },
  nodes: [
    { id: "mic", group: "browser", col: 0, row: 0, label: "Microphone", sub: "and This Mac audio", desc: "The Mac app captures the microphone with its own code. In a source install, the capture helper that setup.sh builds does the same work for the browser page. The audio of this Mac or of one app can join the microphone." },
    { id: "ws", group: "transport", col: 1, row: 0, label: "WebSocket /ws", sub: "PCM16", desc: "The page sends 16 kHz PCM16 audio parts over a WebSocket to the local server. No audio goes out of this Mac." },
    { id: "whisper", group: "local", col: 2, row: 0, label: "ASR engine", sub: "on this Mac", desc: "The selected engine (Whisper or Parakeet) runs on this Mac and changes the audio into text. Whisper transcribes full sentences. Parakeet shows words during the speech. A translator (Canary or Apple) can add a translated line to each segment." },
    { id: "diar", group: "local", col: 3, row: 0, label: "Speaker labels", sub: "diarization", desc: "The app gives each segment a speaker label (Speaker 1, Speaker 2). During a long recording, the app can correct labels that it gave before." },
    { id: "panel", group: "browser", col: 4, row: 0, label: "Live transcript", sub: "right panel", desc: "Segments and a live row show in the Transcript panel, with the speaker labels. The panel follows the last line when you are at the bottom. If you scroll up, an arrow button moves you back to the last line." },
    { id: "ask", group: "browser", col: 5, row: 0, label: "Summary in chat", sub: "then /export", desc: "Ask Claude for a summary of the discussion. Claude receives only the transcript text, not the audio. /export saves the conversation as a text file." }
  ],
  edges: [
    { from: "mic", to: "ws", label: "PCM16" },
    { from: "ws", to: "whisper" },
    { from: "whisper", to: "diar" },
    { from: "diar", to: "panel", label: "segments" },
    { from: "panel", to: "ask" }
  ]
});
