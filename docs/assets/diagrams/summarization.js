/* Architecture - transcript map-reduce condensation before the summary skills */
WSDiagram.mount("summarization-diagram", {
  title: "Transcript condensation: map, then reduce",
  grid: { nodeW: 168, nodeH: 60, gapX: 50, gapY: 40 },
  groups: {
    transport: { label: "Request" }, server: { label: "Condenser" }, model: { label: "Map model" },
    external: { label: "Cloud (Haiku)" }, local: { label: "On-device" }, persist: { label: "Cache" }, tools: { label: "Summary skill" }
  },
  nodes: [
    { id: "req", group: "transport", col: 0, row: 1, label: "POST /api/chat", sub: "transcript in body", desc: "A chat turn carries the whole transcript; the frontend re-sends it on every turn." },
    { id: "gate", group: "server", col: 1, row: 1, label: "Size gate", sub: "> threshold_chars", desc: "Self-gating: only a transcript over threshold_chars is condensed; anything smaller skips every LLM call." },
    { id: "cache", group: "persist", kind: "store", col: 1, row: 0, label: "Condensed cache", sub: "hash + config", desc: "A bounded LRU keyed by transcript hash plus config signature, so a re-sent transcript is condensed once." },
    { id: "chunk", group: "server", col: 2, row: 0, label: "Split into chunks", sub: "overlapping, line-anchored", desc: "The index chunker cuts the transcript into overlapping, structure-aware chunks, capped at max_chunks." },
    { id: "passth", group: "server", col: 2, row: 2, label: "Pass through", sub: "fits the window", desc: "A transcript under the threshold flows through unchanged, with no map, no reduce, and no LLM calls." },
    { id: "map", group: "model", col: 3, row: 0, label: "Map: extract", sub: "one-shot per chunk", desc: "Each chunk gets one non-streaming extraction call that pulls out raw material rather than prose-summarising it." },
    { id: "reduce", group: "server", col: 3, row: 1, label: "Concatenate", sub: "+ NOTE prefix", desc: "Extracts are joined in order behind a NOTE marking them as reduced-fidelity per-segment material." },
    { id: "haiku", group: "external", kind: "external", col: 4, row: 0, label: "Haiku", sub: "cloud invoke", desc: "In cloud mode the map calls fan out to Haiku with bounded concurrency to cut latency." },
    { id: "gemma", group: "local", col: 4, row: 1, label: "Resident model", sub: "loaded_key()", desc: "In local mode the map runs serially on the already-loaded on-device model, never evicting the chat model." },
    { id: "prompt", group: "server", col: 5, row: 1, label: "Into the prompt", sub: "block + tools", desc: "The condensed (or pass-through) text feeds both the transcript-so-far block and the summary tools." },
    { id: "skill", group: "tools", col: 5, row: 2, label: "Reduce: main model", sub: "summary skill", desc: "The main chat model composes the requested summary from that material in its normal turn." }
  ],
  edges: [
    { from: "req", to: "gate" },
    { from: "gate", to: "chunk", label: "oversized" },
    { from: "gate", to: "passth", label: "fits" },
    { from: "gate", to: "cache", label: "hit" },
    { from: "chunk", to: "map" },
    { from: "map", to: "haiku", label: "cloud" },
    { from: "map", to: "gemma", label: "local" },
    { from: "haiku", to: "reduce" },
    { from: "gemma", to: "reduce" },
    { from: "reduce", to: "cache", label: "store" },
    { from: "reduce", to: "prompt" },
    { from: "cache", to: "prompt" },
    { from: "passth", to: "prompt" },
    { from: "prompt", to: "skill", label: "summarize" }
  ]
});
