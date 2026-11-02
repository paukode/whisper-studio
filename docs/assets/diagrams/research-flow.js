/* Tutorial: web research flow (ask, decide, search, fetch, cite) */
WSDiagram.mount("research-flow-diagram", {
  title: "Web research flow",
  grid: { nodeW: 168, nodeH: 60, gapX: 56, gapY: 44 },
  groups: {
    browser: { label: "You" }, server: { label: "The assistant selects" }, external: { label: "Tools" }
  },
  nodes: [
    { id: "ask", group: "browser", col: 0, row: 0.5, label: "Ask a question", sub: "needs current info", desc: "You type a question that needs current facts. You can also ask for sources." },
    { id: "decide", group: "server", col: 1, row: 0.5, label: "Search or answer", sub: "search needed?", desc: "The assistant selects if the training data of the model is sufficient or if it must use the web." },
    { id: "search", group: "external", kind: "external", col: 2, row: 0, label: "web_search", sub: "Tavily", desc: "web_search sends the query to Tavily for current results. It searches only when a Tavily API key is set." },
    { id: "read", group: "external", kind: "external", col: 3, row: 0, label: "web_fetch pages", sub: "readable text", desc: "web_fetch reads the text of the best pages, and of each URL that you paste." },
    { id: "answer", group: "browser", col: 4, row: 0.5, label: "Answer with sources", sub: "linked pages", desc: "The assistant returns an answer with links to the pages that it read. Expand the activity row of the turn to see the query and the results." }
  ],
  edges: [
    { from: "ask", to: "decide" },
    { from: "decide", to: "search", label: "yes" },
    { from: "search", to: "read" },
    { from: "read", to: "answer" },
    { from: "decide", to: "answer", label: "no search" }
  ]
});
