/* Tutorial - indexing a folder and grounding answers in it */
WSDiagram.mount("index-flow-diagram", {
  title: "Index a folder, then ground answers in it",
  grid: { nodeW: 172, nodeH: 60, gapX: 56, gapY: 42 },
  groups: {
    index: { label: "Local index" }, browser: { label: "In the app" }
  },
  nodes: [
    { id: "build", group: "index", col: 0, row: 0, label: "Index a folder", sub: "walk + embed", desc: "Whisper Studio walks the folder, turns each file into text, chunks it, and embeds the chunks into a vector store. Only new or changed files are re-embedded." },
    { id: "graph", group: "index", col: 0, row: 1, label: "Entity graph", sub: "GLiNER / Haiku", desc: "Named entities are extracted per chunk and deduped into canonical nodes. Files and entities that co-occur become linked, viewable as a graph overlay." },
    { id: "query", group: "browser", col: 1, row: 2, label: "Ask a question", sub: "in chat", desc: "You ask a question in chat with one or more indexed folders selected. The question is embedded and searched against the index." },
    { id: "retr", group: "index", col: 2, row: 1, label: "Retrieve passages", sub: "vector + rerank", desc: "Vector search finds the closest chunks, an optional reranker reorders them, and one GraphRAG hop pulls in passages linked through shared entities." },
    { id: "ground", group: "browser", col: 3, row: 1, label: "Grounded answer", sub: "cites files", desc: "The retrieved passages are injected into the prompt as source of truth. Claude answers from them and ends with a Sources list of file links." }
  ],
  edges: [
    { from: "build", to: "graph" },
    { from: "graph", to: "retr" },
    { from: "query", to: "retr" },
    { from: "retr", to: "ground" }
  ]
});
