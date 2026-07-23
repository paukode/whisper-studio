/* Architecture - the memory system (session + workspace memory, WHISPER.md) */
WSDiagram.mount("memory-diagram", {
  title: "How memory is built and injected",
  grid: { nodeW: 168, nodeH: 62, gapX: 54, gapY: 42 },
  groups: {
    server: { label: "Chat turn" }, persist: { label: "Memory stores" }
  },
  nodes: [
    { id: "turn", group: "server", col: 0, row: 0, label: "Chat turn", sub: "one request", desc: "A chat turn ends. Post-query hooks fire as fire-and-forget background tasks." },
    { id: "thresh", group: "server", col: 1, row: 0, label: "Threshold?", sub: "tokens / tool calls", desc: "Characters and tool calls since the last update are counted; an update only runs once a threshold is crossed." },
    { id: "extract", group: "server", col: 2, row: 0, label: "Summarize", sub: "background agent", desc: "The memory_extractor agent (or the local model itself) summarizes recent messages into structured sections." },
    { id: "sess", group: "persist", kind: "store", col: 3, row: 0, label: "Session memory", sub: "Goals/Decisions/Context/Blockers", desc: "A per-session markdown file with four fixed sections, injected as <session-memory>." },
    { id: "wsmem", group: "persist", kind: "store", col: 3, row: 1, label: "Workspace memory", sub: "MEMORY.md + topics", desc: "Files under data/memory/<slug>/: a MEMORY.md index plus per-topic markdown with YAML frontmatter. Injected as <memory-context>." },
    { id: "whmd", group: "persist", kind: "store", col: 3, row: 2, label: "WHISPER.md", sub: "project context", desc: "A project-context file at the workspace root, loaded on every turn like CLAUDE.md. Editable in the app." },
    { id: "inject", group: "server", col: 4, row: 1, label: "Inject into system prompt", sub: "before the model call", desc: "Selected memories, session memory, and WHISPER.md are composed into the system prompt before the model is invoked." }
  ],
  edges: [
    { from: "turn", to: "thresh" },
    { from: "thresh", to: "extract", label: "exceeded" },
    { from: "extract", to: "sess" },
    { from: "sess", to: "inject" },
    { from: "wsmem", to: "inject" },
    { from: "whmd", to: "inject" },
    { from: "inject", to: "turn", label: "next turn" }
  ]
});
