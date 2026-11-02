/* Tutorial - how a skill runs, from your message to the answer */
WSDiagram.mount("skills-flow-diagram", {
  title: "How a skill runs",
  grid: { nodeW: 168, nodeH: 60, gapX: 54, gapY: 40 },
  groups: {
    browser: { label: "Chat" }, server: { label: "Selection" }, tools: { label: "Skill" }
  },
  nodes: [
    { id: "msg", group: "browser", col: 0, row: 0, label: "Your message", sub: "plain English", desc: "You type a request in the chat. You do not name a skill." },
    { id: "match", group: "server", col: 1, row: 0, label: "Relevant skill loads", sub: "auto", desc: "The app gives each enabled skill to the model as a tool, in a short index. Claude selects the skill whose description and triggers match your request." },
    { id: "invoke", group: "tools", col: 2, row: 0, label: "skill_invoke", sub: "runs the skill", desc: "Claude calls the skill. The app applies its Markdown body (or its executor) to your input." },
    { id: "tools", group: "tools", col: 3, row: 0, label: "Uses tools", sub: "web · python · docs", desc: "A skill can use other tools while it runs, for example web search, Python in the sandbox, and document analysis." },
    { id: "result", group: "browser", col: 4, row: 0, label: "Result in chat", sub: "inline trace", desc: "The answer comes into the chat. A small skill indicator shows in the message. Click it to open the trace." }
  ],
  edges: [
    { from: "msg", to: "match" },
    { from: "match", to: "invoke" },
    { from: "invoke", to: "tools" },
    { from: "tools", to: "result" }
  ]
});
