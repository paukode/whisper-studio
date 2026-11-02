/* Architecture: live transcription & diarization pipeline */
WSDiagram.mount("transcription-diagram", {
  title: "Audio in, diarized transcript out",
  grid: { nodeW: 156, nodeH: 62, gapX: 44, gapY: 42 },
  groups: {
    browser: { label: "Browser" }, transport: { label: "Transport" },
    local: { label: "On-device ASR + diarization" }
  },
  nodes: [
    { id: "mic", group: "browser", col: 0, row: 0, label: "Audio capture", sub: "PCM16 16kHz", desc: "The native mic (voice processing off) and optional system or app audio, through the Mac app's bridges or the capture helper of a source install; the browser mic when no native mic is available. The page mixes them into mono 16 kHz PCM16 and streams it as binary WebSocket frames." },
    { id: "ws", group: "transport", col: 1, row: 0, label: "/ws WebSocket", sub: "audio + control", desc: "websocket_endpoint in server/websocket.py. Binary frames carry audio; text frames carry control messages (ping, set_model, set_translate, set_speakers, stop)." },
    { id: "backend", group: "local", col: 2, row: 0, label: "ASR backend", sub: "Parakeet / Whisper", desc: "Resolved once at connect from ?backend= or the global config. Parakeet (streaming) or Whisper large-v3, each on a single-threaded MLX executor. Canary does not transcribe: it only decodes the live translations, whichever engine is transcribing." },
    { id: "vad", group: "local", col: 3, row: 0, label: "VAD / buffer", sub: "trailing silence", desc: "UtteranceBuffer gates audio with webrtcvad (the only speech filter, no energy gate). A settled utterance needs >= 400 ms voiced and a trailing-silence window (350 ms Parakeet, 400 ms Whisper)." },
    { id: "interim", group: "local", col: 4, row: 0, label: "interim + final", sub: "words then settled", desc: "process() emits volatile interim drafts (a re-decode of the growing window, Parakeet only, every chunk) and one authoritative final per utterance, with its audio attached." },
    { id: "ecapa", group: "local", col: 2, row: 1, label: "Speaker embedding", sub: "ReDimNet2 or ECAPA", desc: "Each final utterance (>= 1 s) is embedded into an L2-normalized speaker vector off the decode thread, by the encoder named in speaker_embedder: ReDimNet2-B2 by default, ECAPA-VoxCeleb when selected; a load failure stops labelling with a visible error. Vectors from the two are not comparable, so thresholds and stored voiceprints are filed per encoder." },
    { id: "cluster", group: "local", col: 3, row: 1, label: "Assign / split", sub: "after the final is sent", desc: "A per-connection labeller works through the sent finals in order. It cuts an utterance at a speaker handover, and assign() scores each vector against running cluster members by cosine similarity. A label that differs from the provisional one goes out as speaker_update; a split goes out as transcript_split." },
    { id: "recluster", group: "local", col: 4, row: 1, label: "Re-cluster", sub: "speaker_update", desc: "maybe_recluster() periodically re-runs agglomerative clustering over every embedding and returns label corrections for the utterances that moved." },
    { id: "spa", group: "browser", col: 5, row: 0.5, label: "Live transcript", sub: "SPA", desc: "The React panel renders interims live, appends finals with their provisional speaker + chunk_id, replaces a chunk on transcript_split, and re-labels segments on speaker_update." }
  ],
  edges: [
    { from: "mic", to: "ws" },
    { from: "ws", to: "backend" },
    { from: "backend", to: "vad" },
    { from: "vad", to: "interim" },
    { from: "interim", to: "ecapa", label: "final" },
    { from: "ecapa", to: "cluster" },
    { from: "cluster", to: "recluster" },
    { from: "interim", to: "spa", label: "interim + transcript" },
    { from: "cluster", to: "spa", label: "speaker_update / transcript_split" },
    { from: "recluster", to: "spa", label: "speaker_update" }
  ]
});
