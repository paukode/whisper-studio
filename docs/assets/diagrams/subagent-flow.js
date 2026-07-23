/* Tutorial - how a team fans out and reports back over the event bus */
WSDiagram.mount("subagent-flow-diagram", {
  title: "Spawn a team, watch it report back",
  grid: { nodeW: 168, nodeH: 60, gapX: 56, gapY: 44 },
  groups: {
    browser: { label: "Chat" }, agents: { label: "Agents" },
    server: { label: "Runtime" }
  },
  nodes: [
    { id: "task", group: "browser", col: 0, row: 0.5, label: "Big task", sub: "one prompt", desc: "You describe work that splits cleanly into parts, or type /subagent for a single focused helper." },
    { id: "spawn", group: "agents", col: 1, row: 0.5, label: "spawn_agent / team_create", sub: "from chat", desc: "Claude decides to parallelize. team_create fans out several agents at once; spawn_agent launches one independent agent." },
    { id: "a1", group: "agents", col: 2, row: 0, label: "Agent A", sub: "explore", desc: "A read-only agent that searches and reads to gather evidence, then returns concise findings." },
    { id: "a2", group: "agents", col: 2, row: 1, label: "Agent B", sub: "verify", desc: "Runs its own tool loop, checks the work, and ends with VERDICT: PASS, FAIL, or PARTIAL." },
    { id: "bus", group: "server", kind: "store", col: 3, row: 0.5, label: "Event bus", sub: "team_progress", desc: "Each agent publishes started, turn_start, tool_call, tool_result, and completed phases to a per-session channel." },
    { id: "report", group: "browser", col: 4, row: 0.5, label: "Team report card", sub: "live + final", desc: "One collapsible card per team. Rows update in real time and expand to the agent's event log and final output." }
  ],
  edges: [
    { from: "task", to: "spawn" },
    { from: "spawn", to: "a1", label: "fan out" },
    { from: "spawn", to: "a2" },
    { from: "a1", to: "bus", label: "publish" },
    { from: "a2", to: "bus" },
    { from: "bus", to: "report", label: "team_progress" }
  ]
});
