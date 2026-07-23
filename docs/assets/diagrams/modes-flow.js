/* Tutorial - model mode picks where the indexing/RAG stack runs */
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
    { id: "pick", group: "browser", col: 0, row: 1, label: "Pick a mode", sub: "Settings > Model Mode", desc: "One dropdown chooses where embeddings, reranking, and entity extraction run. It is separate from your chat-model choice in the toolbar." },
    { id: "cloud", group: "external", kind: "external", col: 1, row: 0, label: "Cloud", sub: "Cohere on Bedrock", desc: "Default. Cohere Embed v4 and Rerank 3.5 on Amazon Bedrock, Claude Haiku for entity extraction. No on-device weights, so the install stays lean." },
    { id: "hybrid", group: "server", col: 1, row: 1, label: "Hybrid", sub: "per capability", desc: "Pick a backend per capability (embed, rerank, ner, index_llm). Unset capabilities fall back to the cloud backend. The on-device weights are pulled so you can choose them." },
    { id: "local", group: "local", col: 1, row: 2, label: "Local", sub: "Qwen3 · GLiNER", desc: "Everything on-device: Qwen3 embeddings, the Qwen3 Reranker, and GLiNER for entities. Around 16 GB of weights, no Bedrock calls for indexing." },
    { id: "rag", group: "index", col: 2, row: 1, label: "Index / RAG runs there", sub: "embed · rerank · entities", desc: "The chosen mode drives the whole indexing and retrieval stack. Each embedder keeps its own index, so switching mode never rebuilds the other." }
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
