/* Architecture - workspace index: build (walk -> store) and query (question -> grounding) */
WSDiagram.mount("index-diagram", {
  title: "Index build and query pipelines",
  grid: { nodeW: 158, nodeH: 60, gapX: 46, gapY: 44 },
  groups: {
    index: { label: "Index pipeline" }, persist: { label: "Storage" },
    browser: { label: "Query" }, server: { label: "Chat" }
  },
  zones: [
    { label: "Build (offline, incremental)", group: "index", col: 0, row: 0, cols: 5, rows: 1 },
    { label: "Query (per question)", group: "browser", col: 0, row: 1, cols: 5, rows: 1 }
  ],
  nodes: [
    { id: "walk", group: "index", col: 0, row: 0, label: "Walk workspace", sub: "hash + skip vendor", desc: "os.walk skips .git, node_modules, __pycache__, .venv, dist. A size+mtime gate, then a SHA1 content hash, means only new or changed files are re-embedded." },
    { id: "chunk", group: "index", col: 1, row: 0, label: "Chunk", sub: "headings / code, ~400 tok", desc: "Structure-aware chunking on markdown headings and code definitions, budgeted to ~400 tokens with a 64-token overlap so a chunk stays under the embedder's 512-token window." },
    { id: "embed", group: "index", col: 2, row: 0, label: "Embed", sub: "Qwen3 / Cohere", desc: "Qwen3-Embedding-0.6B on device (1024-d, L2-normalized, MPS/CPU), or Cohere Embed v4 on Bedrock in cloud mode. Embedded in batches." },
    { id: "ent", group: "index", col: 3, row: 0, label: "Entities", sub: "GLiNER / Haiku", desc: "Zero-shot NER (GLiNER local, or Claude Haiku in cloud mode) tags each chunk's entities. Distinct entities become graph nodes." },
    { id: "rel", group: "index", col: 4, row: 0, label: "Relations", sub: "typed edges (optional)", desc: "Optional per-file typed relations (works_at, cites, depends_on) via Haiku or on-device Gemma. Endpoints are validated against the entity list, so the LLM cannot invent nodes." },
    { id: "store", group: "persist", kind: "store", col: 5, row: 0.5, label: "SQLite + sqlite-vec", sub: "vectors as BLOB", desc: "Per-workspace, per-backend SQLite DB. Vectors are float32 BLOBs (the source of truth); a sqlite-vec vchunks index is rebuilt on any write-generation change, with a numpy brute-force fallback." },

    { id: "q", group: "browser", col: 0, row: 1, label: "Query", sub: "the question", desc: "The user's question (optionally folded with recent turns) becomes the retrieval query." },
    { id: "knn", group: "index", col: 1, row: 1, label: "Vector KNN", sub: "sqlite-vec", desc: "The question is embedded with an instruction prefix, then top-k cosine neighbours are fetched via sqlite-vec (numpy fallback). An FTS5/BM25 keyword leg catches exact terms." },
    { id: "rerank", group: "index", col: 2, row: 1, label: "Rerank", sub: "cross-encoder (opt)", desc: "Optional Qwen3-Reranker cross-encoder (or Cohere Rerank on Bedrock) rescored each (question, passage) pair. Best-effort behind the rag_reranker flag." },
    { id: "hop", group: "index", col: 3, row: 1, label: "GraphRAG hop", sub: "shared entities", desc: "One hop along the entity graph: chunks that share entity nodes with the top matches, ranked by how many they share, come in as supporting context." },
    { id: "ground", group: "server", col: 4, row: 1, label: "Passages / grounding", sub: "cited context block", desc: "Deduped, per-document-capped, score-floored passages become a cited Markdown block injected into the chat prompt, surfaced as a grounding chip." }
  ],
  edges: [
    { from: "walk", to: "chunk" },
    { from: "chunk", to: "embed" },
    { from: "embed", to: "ent" },
    { from: "ent", to: "rel" },
    { from: "rel", to: "store", label: "write" },
    { from: "embed", to: "store", label: "vectors" },
    { from: "q", to: "knn", label: "embed" },
    { from: "store", to: "knn", label: "search" },
    { from: "knn", to: "rerank" },
    { from: "rerank", to: "hop" },
    { from: "hop", to: "ground" }
  ]
});
