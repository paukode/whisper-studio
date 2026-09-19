/* Architecture - what an agent run leaves behind, and how it gets read */
WSDiagram.mount("agent-recorder-diagram", {
  title: "One agent run, from first round to the parent's answer",
  grid: { nodeW: 168, nodeH: 60, gapX: 54, gapY: 44 },
  groups: {
    agents: { label: "Agent runtime" },
    persist: { label: "On disk" },
    transport: { label: "Delivery" },
    browser: { label: "Chat" }
  },
  nodes: [
    { id: "start", group: "agents", col: 0, row: 0.5, label: "run_agent", sub: "record opens first", desc: "Before the first model call the journal directory and the background-task row exist, so a run that is cancelled, crashes or outlives its parent turn still leaves a record." },
    { id: "round", group: "agents", col: 1, row: 0, label: "Every round", sub: "events + checkpoint", desc: "Each progress event is appended to events.jsonl, and the whole API message list is rewritten to messages.json at every round start." },
    { id: "final", group: "agents", col: 1, row: 1, label: "Final round", sub: "report template", desc: "The last round is reserved for the report. The turn cap, 90 percent of the time budget, or 90 percent of the cost cap all start it, with the findings/evidence/confidence template attached." },
    { id: "journal", group: "persist", kind: "store", col: 2, row: 0.5, label: "Journal directory", sub: "meta, events, messages, report", desc: "storage/agents/<session>/<agent-id>/: meta.json, events.jsonl, messages.json, report.md. task_output reads it live or long after." },
    { id: "live", group: "transport", col: 3, row: 0, label: "Tool result", sub: "a turn is waiting", desc: "The normal case: the report comes back as the spawn_agent or team_create tool result, with the relay note telling the assistant the user has not seen it." },
    { id: "row", group: "transport", col: 3, row: 1, label: "Report row", sub: "no turn to read it", desc: "You pressed Stop, or the app restarted, or the agent ran detached. The report is persisted into the session as an agent report row plus a notification." },
    { id: "wake", group: "transport", col: 4, row: 1, label: "Wake the parent", sub: "mid-turn or one round", desc: "A live turn receives the reports mid-turn. With no turn running, a single no-tools round answers from them." },
    { id: "chat", group: "browser", col: 5, row: 0.5, label: "Report card + answer", sub: "in the conversation", desc: "The report card renders each agent's report; the wake turn's reply appears as an assistant bubble labelled as written from the agents' reports." }
  ],
  edges: [
    { from: "start", to: "round" },
    { from: "round", to: "final", label: "budget nearly spent" },
    { from: "round", to: "journal", label: "append" },
    { from: "final", to: "journal", label: "report.md" },
    { from: "journal", to: "live" },
    { from: "journal", to: "row" },
    { from: "row", to: "wake" },
    { from: "live", to: "chat" },
    { from: "wake", to: "chat" }
  ]
});
