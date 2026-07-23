/* Tutorial - how a skill runs, from your message to the answer */
WSDiagram.mount("skills-flow-diagram", {
  title: "How a skill runs",
  grid: { nodeW: 168, nodeH: 60, gapX: 54, gapY: 40 },
  groups: {
    browser: { label: "Chat" }, server: { label: "Selection" }, tools: { label: "Skill" }
  },
  nodes: [
    { id: "msg", group: "browser", col: 0, row: 0, label: "Your message", sub: "plain English", desc: "You type a request in chat. You don't name a skill." },
    { id: "match", group: "server", col: 1, row: 0, label: "Relevant skill loads", sub: "auto", desc: "Each enabled skill is offered to the model as a tool. Claude picks the one whose description and triggers fit your request." },
    { id: "invoke", group: "tools", col: 2, row: 0, label: "skill_invoke", sub: "runs the skill", desc: "Claude calls the skill. Its Markdown body (or executor) is applied to your input." },
    { id: "tools", group: "tools", col: 3, row: 0, label: "Uses tools", sub: "web · python · docs", desc: "A skill may reach for other tools while it runs: web search, sandboxed Python, document analysis." },
    { id: "result", group: "browser", col: 4, row: 0, label: "Result in chat", sub: "inline trace", desc: "The answer streams back. A small skill indicator appears; click it to open the trace." }
  ],
  edges: [
    { from: "msg", to: "match" },
    { from: "match", to: "invoke" },
    { from: "invoke", to: "tools" },
    { from: "tools", to: "result" }
  ]
});
