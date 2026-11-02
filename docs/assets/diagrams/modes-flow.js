/* Tutorial: model mode selects where the indexing/RAG stack and chat run */
WSDiagram.mount("modes-flow-diagram", {
  title: "Model mode routes the index / RAG stack",
  grid: { nodeW: 168, nodeH: 62, gapX: 66, gapY: 34 },
  groups: {
    browser: { label: "You" },
    external: { label: "Cloud (Bedrock)" },
    server: { label: "Hybrid" },
    local: { label: "On-device" },
    index: { label: "Index / RAG" }
  },
  nodes: [
    { id: "pick", group: "browser", col: 0, row: 1, label: "Pick a mode", sub: "Settings > Model mode", desc: "One list selects where embeddings, reranking, and entity extraction run. It also sets which chat models you can use: Cloud shows cloud models, Local only on-device models, Hybrid both." },
    { id: "cloud", group: "external", kind: "external", col: 1, row: 0, label: "Cloud", sub: "Cohere on Bedrock", desc: "Cohere Embed v4 and Rerank 3.5 on Amazon Bedrock, and Claude Haiku for entity extraction. There are no on-device weights, so the install stays small. Chat uses cloud models." },
    { id: "hybrid", group: "server", col: 1, row: 1, label: "Hybrid", sub: "per capability", desc: "Select a backend for each function (embed, rerank, ner, index_llm). A function without a value uses the cloud backend. The on-device weights download at the first use. Chat can use cloud and on-device models." },
    { id: "local", group: "local", col: 1, row: 2, label: "Local", sub: "Qwen3 · GLiNER", desc: "The default of a new install. The index stack is on-device: Qwen3 embeddings, the Qwen3 Reranker, and GLiNER for entities. The weights download at the first index build. Chat runs only on an on-device model, which you install from Discover." },
    { id: "rag", group: "index", col: 2, row: 1, label: "Index / RAG runs there", sub: "embed · rerank · entities", desc: "The selected mode controls the full indexing and retrieval stack. Each embedder keeps its own index, so a mode change does not rebuild the other index." }
  ],
  edges: [
    { from: "pick", to: "cloud" },
    { from: "pick", to: "hybrid" },
    { from: "pick", to: "local" },
    { from: "cloud", to: "rag" },
    { from: "hybrid", to: "rag" },
    { from: "local", to: "rag" }
  ]
});
