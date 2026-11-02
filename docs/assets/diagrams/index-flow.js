/* Tutorial - indexing a folder and grounding answers in it */
WSDiagram.mount("index-flow-diagram", {
  title: "Index a folder, then ground answers in it",
  grid: { nodeW: 172, nodeH: 60, gapX: 56, gapY: 42 },
  groups: {
    index: { label: "Local index" }, browser: { label: "In the app" }
  },
  nodes: [
    { id: "build", group: "index", col: 0, row: 0, label: "Index a folder", sub: "walk + embed", desc: "Whisper Studio reads the folder, changes each file into text, divides the text into passages, and writes an embedding of each passage to a vector store. A new build reads only new and changed files. Hidden folders and folders such as node_modules and build are skipped." },
    { id: "graph", group: "index", col: 0, row: 1, label: "Entity graph", sub: "GLiNER / Haiku", desc: "The build extracts named entities from each passage and joins the spellings of one name into one entry. Files that share an entity become linked. The index explorer shows these links." },
    { id: "query", group: "browser", col: 1, row: 2, label: "Ask a question", sub: "in chat", desc: "You ask a question in the chat with one or more indexed folders selected (More, Index search). The app makes an embedding of the question and searches the index." },
    { id: "retr", group: "index", col: 2, row: 1, label: "Retrieve passages", sub: "vector + keyword", desc: "A vector search and a keyword search find the nearest passages. A reranker (off by default) can sort them again. One GraphRAG step adds passages that share an entity." },
    { id: "ground", group: "browser", col: 3, row: 1, label: "Grounded answer", sub: "cites files", desc: "The app adds the passages to the prompt as the source of truth. Claude answers from them and ends with a Sources list of file links." }
  ],
  edges: [
    { from: "build", to: "graph" },
    { from: "graph", to: "retr" },
    { from: "query", to: "retr" },
    { from: "retr", to: "ground" }
  ]
});
