/* Architecture - live transcription & diarization pipeline */
WSDiagram.mount("transcription-diagram", {
  title: "Audio in, diarized transcript out",
  grid: { nodeW: 156, nodeH: 62, gapX: 44, gapY: 42 },
  groups: {
    browser: { label: "Browser" }, transport: { label: "Transport" },
    local: { label: "On-device ASR + diarization" }
  },
  nodes: [
    { id: "mic", group: "browser", col: 0, row: 0, label: "Browser mic", sub: "PCM16 16kHz", desc: "The recorder captures mono 16 kHz PCM16 and streams it as binary WebSocket frames." },
    { id: "ws", group: "transport", col: 1, row: 0, label: "/ws WebSocket", sub: "audio + control", desc: "websocket_endpoint in server/websocket.py. Binary frames carry audio; text frames carry control messages (ping, set_backend, set_speakers, stop)." },
    { id: "backend", group: "local", col: 2, row: 0, label: "ASR backend", sub: "Parakeet / Whisper", desc: "Resolved once at connect from ?backend= or the global config. Parakeet (streaming) or Whisper, both on a single-threaded MLX executor." },
    { id: "vad", group: "local", col: 3, row: 0, label: "VAD / buffer", sub: "trailing silence", desc: "UtteranceBuffer gates audio with webrtcvad. A settled utterance needs >= 400 ms voiced and a trailing-silence window (350 ms Parakeet, 400 ms Whisper)." },
    { id: "interim", group: "local", col: 4, row: 0, label: "interim + final", sub: "words then settled", desc: "process() emits volatile interim drafts (a re-decode of the growing window) and one authoritative final per utterance, with its audio attached." },
    { id: "ecapa", group: "local", col: 2, row: 1, label: "ECAPA embedding", sub: "speaker vector", desc: "Each final utterance (>= 1 s) is embedded by the ECAPA-VoxCeleb encoder into an L2-normalized speaker vector, off the decode thread." },
    { id: "cluster", group: "local", col: 3, row: 1, label: "Assign speaker", sub: "cosine to centroid", desc: "assign() scores the vector against running cluster members by cosine similarity and labels the utterance immediately, so the transcript never waits." },
    { id: "recluster", group: "local", col: 4, row: 1, label: "Re-cluster", sub: "speaker_update", desc: "maybe_recluster() periodically re-runs agglomerative clustering over every embedding and returns label corrections for the utterances that moved." },
    { id: "spa", group: "browser", col: 5, row: 0.5, label: "Live transcript", sub: "SPA", desc: "The React panel renders interims live, appends finals with their speaker + chunk_id, and retroactively re-labels segments on speaker_update." }
  ],
  edges: [
    { from: "mic", to: "ws" },
    { from: "ws", to: "backend" },
    { from: "backend", to: "vad" },
    { from: "vad", to: "interim" },
    { from: "interim", to: "ecapa", label: "final" },
    { from: "ecapa", to: "cluster" },
    { from: "cluster", to: "recluster" },
    { from: "interim", to: "spa", label: "interim" },
    { from: "recluster", to: "spa", label: "speaker_update" }
  ]
});
