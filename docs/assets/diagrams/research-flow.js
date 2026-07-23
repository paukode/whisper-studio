/* Tutorial - web research flow (ask, decide, search, fetch, cite) */
WSDiagram.mount("research-flow-diagram", {
  title: "Web research flow",
  grid: { nodeW: 168, nodeH: 60, gapX: 56, gapY: 44 },
  groups: {
    browser: { label: "You" }, server: { label: "Claude decides" }, external: { label: "Tools" }
  },
  nodes: [
    { id: "ask", group: "browser", col: 0, row: 0.5, label: "Ask a question", sub: "needs current info", desc: "You type a question that needs up-to-date facts, and (optionally) ask for sources." },
    { id: "decide", group: "server", col: 1, row: 0.5, label: "Claude decides", sub: "search needed?", desc: "Claude judges whether its own knowledge is enough or whether the web is required." },
    { id: "search", group: "external", kind: "external", col: 2, row: 0, label: "web_search", sub: "Tavily", desc: "web_search queries Tavily for current results. Only available when a Tavily API key is set." },
    { id: "read", group: "external", kind: "external", col: 3, row: 0, label: "web_fetch pages", sub: "readable text", desc: "web_fetch pulls the readable text of the most promising pages (and any URL you paste)." },
    { id: "answer", group: "browser", col: 4, row: 0.5, label: "Cited answer", sub: "footnoted links", desc: "Claude streams a normal answer with footnoted links back to its sources." }
  ],
  edges: [
    { from: "ask", to: "decide" },
    { from: "decide", to: "search", label: "yes" },
    { from: "search", to: "read" },
    { from: "read", to: "answer" },
    { from: "decide", to: "answer", label: "already knows" }
  ]
});
